import errno
import json
import logging
import threading
import time
from typing import Any, Dict, Optional

import rados

from .writer_state import WRITER_OBJECT_NAME, WRITER_OBJECT_PREFIX


log = logging.getLogger(__name__)


class WriterNotifier:
    def __init__(self, ioctx):
        self.ioctx = ioctx

    @staticmethod
    def instance_object(instance_id: str) -> str:
        return f'{WRITER_OBJECT_PREFIX}.{instance_id}'

    @staticmethod
    def _payload(ack) -> str:
        payload = ack[2]
        if isinstance(payload, bytes):
            return payload.decode('utf-8')
        if isinstance(payload, str):
            return payload
        raise ValueError('invalid acknowledgment payload')

    @staticmethod
    def _decode_ack(ack, expected: Dict[str, Any]) -> Optional[int]:
        try:
            response = json.loads(WriterNotifier._payload(ack))
            if str(ack[0]) != expected['instance_id'] or \
               type(response.get('version')) is not int or \
               response.get('version') != 1 or \
               response.get('operation_id') != expected['operation_id'] or \
               response.get('peer_uuid') != expected['peer_uuid'] or \
               response.get('path') != expected['path'] or \
               type(response.get('assignment_epoch')) is not int or \
               response.get('assignment_epoch') != \
                    expected['assignment_epoch'] or \
               response.get('process_incarnation') != \
                    expected['process_incarnation']:
                return None
            result = response.get('result')
            if type(result) is not int or result > 0:
                return -errno.EPROTO
            if expected['mode'] == 'release' and \
               response.get('quiesced') is not True:
                return -errno.EBUSY
            return result
        except (AttributeError, IndexError, KeyError, TypeError,
                UnicodeDecodeError, ValueError):
            return None

    def notify(self, instance_id: str, message: Dict[str, Any],
               callback) -> None:
        payload = json.dumps(message, sort_keys=True,
                             separators=(',', ':'))

        def handle_notify(_, result, acks, timeouts):
            try:
                log.debug('writer notify result=%s acks=%s timeouts=%s',
                          result, acks, timeouts)
                if result < 0:
                    callback_result = result
                else:
                    callback_result = None
                    for ack in acks or []:
                        ack_result = self._decode_ack(ack, message)
                        if ack_result is not None:
                            callback_result = ack_result
                            break
                    if callback_result is None:
                        callback_result = -errno.ETIMEDOUT if timeouts \
                            else -errno.EPROTO
            except Exception:
                log.exception('writer notification callback failed')
                callback_result = -errno.EPROTO
            try:
                callback(callback_result)
            except Exception:
                log.exception('writer notification completion failed')

        try:
            self.ioctx.aio_notify(self.instance_object(instance_id),
                                  handle_notify, msg=payload)
        except rados.Error as e:
            error_number = getattr(e, 'errno', errno.EIO)
            callback(error_number if error_number < 0 else -error_number)


class WriterInstanceWatcher:
    INSTANCE_TIMEOUT = 30
    NOTIFY_INTERVAL = 1

    def __init__(self, ioctx, listener):
        self.ioctx = ioctx
        self.listener = listener
        self.instances = {}  # type: Dict[str, Dict[str, Any]]
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.stopping = False
        self.callbacks = 0
        self.notify_task = None  # type: Optional[threading.Timer]
        self._schedule()

    def _schedule(self) -> None:
        if self.stopping:
            return
        assert self.notify_task is None
        self.notify_task = threading.Timer(self.NOTIFY_INTERVAL, self.notify)
        self.notify_task.daemon = True
        self.notify_task.start()

    def stop(self) -> None:
        with self.lock:
            self.stopping = True
            task = self.notify_task
            self.notify_task = None
        if task:
            task.cancel()
            task.join()
        with self.lock:
            self.cond.wait_for(lambda: self.callbacks == 0)

    @staticmethod
    def _decode_instance(ack) -> Dict[str, Any]:
        instance_id = str(ack[0])
        response = json.loads(WriterNotifier._payload(ack))
        required = ('addr', 'daemon_id', 'process_incarnation',
                    'destination_client_identity', 'protocol_version')
        if type(response.get('version')) is not int or \
           response.get('version') != 1 or \
           any(field not in response for field in required) or \
           type(response.get('protocol_version')) is not int:
            raise ValueError('invalid writer discovery acknowledgment')
        filesystems = response.get('filesystems', [])
        features = response.get('features', [])
        if not isinstance(filesystems, list) or not isinstance(features, list):
            raise ValueError('invalid writer compatibility data')
        if any(not isinstance(value, str)
               for values in (filesystems, features) for value in values):
            raise ValueError('invalid writer compatibility data')
        return {
            'instance_id': instance_id,
            'version': 1,
            'addr': str(response['addr']),
            'daemon_id': str(response['daemon_id']),
            'process_incarnation': str(response['process_incarnation']),
            'protocol_version': response['protocol_version'],
            'destination_client_identity':
                response['destination_client_identity'],
            'filesystems': filesystems,
            'features': features,
        }

    def handle_notify(self, _, result, acks, timeouts) -> None:
        now = time.time()
        added = {}
        removed = {}
        try:
            with self.lock:
                if self.stopping:
                    return
                if result >= 0:
                    for ack in acks or []:
                        try:
                            record = self._decode_instance(ack)
                        except (AttributeError, IndexError, TypeError,
                                UnicodeDecodeError, ValueError) as e:
                            log.warning(
                                'ignoring invalid writer discovery ack: %s',
                                e)
                            continue
                        instance_id = record.pop('instance_id')
                        current = dict(record)
                        current['seen'] = now
                        self.instances[instance_id] = current
                        # The listener decides whether this is a new identity.
                        # Reporting every response also retries a failed
                        # persistence update without treating persisted state
                        # as liveness.
                        added[instance_id] = dict(record)
                for instance_id, record in list(self.instances.items()):
                    if now - record['seen'] > self.INSTANCE_TIMEOUT:
                        removed[instance_id] = dict(record)
                        self.instances.pop(instance_id)
            if added or removed:
                self.listener(added, removed)
        except Exception:
            log.exception('writer discovery callback failed')
        finally:
            with self.lock:
                self.callbacks -= 1
                assert self.callbacks >= 0
                if not self.stopping:
                    self._schedule()
                self.cond.notify_all()

    def notify(self) -> None:
        with self.lock:
            self.notify_task = None
            if self.stopping:
                return
            self.callbacks += 1
        try:
            self.ioctx.aio_notify(WRITER_OBJECT_NAME,
                                  self.handle_notify)
        except Exception as e:
            with self.lock:
                self.callbacks -= 1
                log.warning('writer discovery notify failed: %s', e)
                if not self.stopping:
                    self._schedule()
                self.cond.notify_all()
