import errno
import json
import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

import rados

from .exception import MirrorException


log = logging.getLogger(__name__)

WRITER_OBJECT_NAME = 'cephfs_mirror_writer'
WRITER_OBJECT_PREFIX = WRITER_OBJECT_NAME
WRITER_INSTANCE_PREFIX = 'instance_'
WRITER_DIRECTORY_PREFIX = 'dir_map_'
WRITER_EPOCH_PREFIX = 'epoch_'

SCHEMA_KEY = '_schema'
REVISION_KEY = '_revision'
SCHEMA_VERSION = 1
MAX_RETURN = 256
MAX_EPOCH = (1 << 64) - 1


def encode_json(value: Dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')


def decode_json(value: bytes, description: str) -> Dict[str, Any]:
    try:
        decoded = json.loads(value.decode('utf-8'))
    except (UnicodeDecodeError, ValueError, TypeError) as e:
        raise MirrorException(-errno.EUCLEAN,
                              f'invalid {description}: {e}')
    if not isinstance(decoded, dict):
        raise MirrorException(-errno.EUCLEAN,
                              f'invalid {description}: expected an object')
    return decoded


class WriterState:
    """Versioned, manager-owned state in the destination metadata pool."""

    def __init__(self, ioctx):
        self.ioctx = ioctx
        self.revision = 0
        self.stale = False
        self.lock = threading.Lock()

    @staticmethod
    def instance_key(instance_id: str) -> str:
        return f'{WRITER_INSTANCE_PREFIX}{instance_id}'

    @staticmethod
    def directory_suffix(peer_uuid: str, dir_path: str) -> str:
        return f'{peer_uuid}:{dir_path}'

    @staticmethod
    def directory_key(peer_uuid: str, dir_path: str) -> str:
        return f'{WRITER_DIRECTORY_PREFIX}' \
               f'{WriterState.directory_suffix(peer_uuid, dir_path)}'

    @staticmethod
    def epoch_key(peer_uuid: str, dir_path: str) -> str:
        return f'{WRITER_EPOCH_PREFIX}' \
               f'{WriterState.directory_suffix(peer_uuid, dir_path)}'

    @staticmethod
    def split_directory_key(key: str, prefix: str) -> Tuple[str, str]:
        suffix = key[len(prefix):]
        try:
            peer_uuid, dir_path = suffix.split(':', 1)
        except ValueError:
            raise MirrorException(-errno.EUCLEAN,
                                  f'invalid writer OMAP key {key}')
        if not peer_uuid or not dir_path.startswith('/'):
            raise MirrorException(-errno.EUCLEAN,
                                  f'invalid writer OMAP key {key}')
        return peer_uuid, dir_path

    @staticmethod
    def _error_code(error: Exception) -> int:
        error_number = getattr(error, 'errno', None)
        if error_number is None and error.args:
            error_number = error.args[0]
        if not isinstance(error_number, int):
            return -errno.EIO
        return error_number if error_number < 0 else -error_number

    def initialize(self) -> None:
        schema = encode_json({'format': WRITER_OBJECT_NAME,
                              'version': SCHEMA_VERSION})
        try:
            with rados.WriteOpCtx() as write_op:
                write_op.new(rados.LIBRADOS_CREATE_EXCLUSIVE)
                self.ioctx.set_omap(write_op,
                                    (SCHEMA_KEY, REVISION_KEY),
                                    (schema, b'0'))
                self.ioctx.operate_write_op(write_op, WRITER_OBJECT_NAME)
            self.revision = 0
            self.stale = False
            return
        except rados.Error as e:
            if self._error_code(e) != -errno.EEXIST:
                raise MirrorException(self._error_code(e),
                                      'failed to create writer state object')
        self._load_header()

    def _read_keys(self, keys: Tuple[str, ...]) -> Dict[str, bytes]:
        try:
            with rados.ReadOpCtx() as read_op:
                iterator, result = self.ioctx.get_omap_vals_by_keys(read_op,
                                                                    keys)
                if result != 0:
                    raise MirrorException(-errno.EIO,
                                          'failed to read writer state')
                self.ioctx.operate_read_op(read_op, WRITER_OBJECT_NAME)
                return dict(iterator)
        except rados.Error as e:
            raise MirrorException(self._error_code(e),
                                  'failed to read writer state')

    def _load_header(self) -> None:
        values = self._read_keys((SCHEMA_KEY, REVISION_KEY))
        if SCHEMA_KEY not in values or REVISION_KEY not in values:
            raise MirrorException(-errno.EUCLEAN,
                                  'writer state header is incomplete')
        schema = decode_json(values[SCHEMA_KEY], 'writer state schema')
        if schema.get('format') != WRITER_OBJECT_NAME:
            raise MirrorException(-errno.EUCLEAN,
                                  'writer state object has the wrong format')
        if type(schema.get('version')) is not int or \
           schema.get('version') != SCHEMA_VERSION:
            raise MirrorException(-errno.EOPNOTSUPP,
                                  'unsupported writer state version')
        try:
            revision = int(values[REVISION_KEY])
        except (TypeError, ValueError):
            raise MirrorException(-errno.EUCLEAN,
                                  'invalid writer state revision')
        if revision < 0:
            raise MirrorException(-errno.EUCLEAN,
                                  'invalid writer state revision')
        self.revision = revision

    def _load_prefix(self, prefix: str) -> Dict[str, bytes]:
        values = {}  # type: Dict[str, bytes]
        start = ''
        try:
            while True:
                with rados.ReadOpCtx() as read_op:
                    iterator, result = self.ioctx.get_omap_vals(
                        read_op, start, prefix, MAX_RETURN)
                    if result != 0:
                        raise MirrorException(-errno.EIO,
                                              'failed to read writer state')
                    self.ioctx.operate_read_op(read_op, WRITER_OBJECT_NAME)
                    page = dict(iterator)
                if not page:
                    break
                values.update(page)
                start = sorted(page)[-1]
            return values
        except rados.Error as e:
            raise MirrorException(self._error_code(e),
                                  'failed to read writer state')

    @staticmethod
    def _validate_version(record: Dict[str, Any], description: str) -> None:
        if type(record.get('version')) is not int or \
           record.get('version') != SCHEMA_VERSION:
            raise MirrorException(-errno.EOPNOTSUPP,
                                  f'unsupported {description} version')

    def load(self) -> Tuple[Dict[str, Dict[str, Any]],
                            Dict[Tuple[str, str], Dict[str, Any]],
                            Dict[Tuple[str, str], int]]:
        with self.lock:
            for _ in range(5):
                self._load_header()
                loaded_revision = self.revision
                try:
                    instances = {}
                    directories = {}
                    epochs = {}

                    for key, value in self._load_prefix(
                            WRITER_INSTANCE_PREFIX).items():
                        instance_id = key[len(WRITER_INSTANCE_PREFIX):]
                        record = decode_json(
                            value, f'writer instance {instance_id}')
                        self._validate_version(
                            record, f'writer instance {instance_id}')
                        required = ('addr', 'daemon_id',
                                    'process_incarnation',
                                    'destination_client_identity')
                        if not instance_id or any(
                                field not in record for field in required) or \
                           any(not isinstance(record[field], str)
                               for field in ('addr', 'daemon_id',
                                             'process_incarnation')):
                            raise MirrorException(
                                -errno.EUCLEAN,
                                f'invalid writer instance {instance_id}')
                        instances[instance_id] = record

                    for key, value in self._load_prefix(
                            WRITER_DIRECTORY_PREFIX).items():
                        peer_uuid, dir_path = self.split_directory_key(
                            key, WRITER_DIRECTORY_PREFIX)
                        record = decode_json(
                            value,
                            f'writer directory {peer_uuid}:{dir_path}')
                        self._validate_version(
                            record,
                            f'writer directory {peer_uuid}:{dir_path}')
                        required = ('instance_id', 'assignment_epoch',
                                    'last_shuffled', 'purging')
                        if any(field not in record for field in required) or \
                           not isinstance(record['instance_id'], str) or \
                            type(record['assignment_epoch']) is not int or \
                           record['assignment_epoch'] < 0 or \
                           record['assignment_epoch'] > MAX_EPOCH or \
                            type(record['last_shuffled']) not in \
                                (int, float) or \
                           not isinstance(record['purging'], bool) or \
                           not isinstance(record.get('reassigning', False),
                                          bool) or \
                           (record['instance_id'] and
                            ('process_incarnation' not in record or
                             'destination_client_identity' not in record)):
                            raise MirrorException(
                                -errno.EUCLEAN,
                                f'invalid writer directory '
                                f'{peer_uuid}:{dir_path}')
                        directories[(peer_uuid, dir_path)] = record

                    for key, value in self._load_prefix(
                            WRITER_EPOCH_PREFIX).items():
                        peer_uuid, dir_path = self.split_directory_key(
                            key, WRITER_EPOCH_PREFIX)
                        try:
                            epoch = int(value)
                        except (TypeError, ValueError):
                            raise MirrorException(
                                -errno.EUCLEAN,
                                f'invalid writer epoch '
                                f'{peer_uuid}:{dir_path}')
                        if epoch < 0 or epoch > MAX_EPOCH:
                            raise MirrorException(
                                -errno.EUCLEAN,
                                f'invalid writer epoch '
                                f'{peer_uuid}:{dir_path}')
                        epochs[(peer_uuid, dir_path)] = epoch

                    for key, record in directories.items():
                        if record['assignment_epoch'] != epochs.get(key, -1):
                            raise MirrorException(
                                -errno.EUCLEAN,
                                f'writer epoch mismatch for '
                                f'{key[0]}:{key[1]}')
                except MirrorException:
                    self._load_header()
                    if self.revision != loaded_revision:
                        continue
                    raise

                self._load_header()
                if self.revision != loaded_revision:
                    continue
                self.stale = False
                return instances, directories, epochs
            self.stale = True
            raise MirrorException(-errno.EAGAIN,
                                  'writer state changed while loading')

    def _mutate(self, updates: Dict[str, bytes], removals: List[str]) -> None:
        with self.lock:
            next_revision = self.revision + 1
            try:
                with rados.WriteOpCtx() as write_op:
                    write_op.omap_cmp(REVISION_KEY, str(self.revision),
                                      rados.LIBRADOS_CMPXATTR_OP_EQ)
                    if updates:
                        self.ioctx.set_omap(write_op,
                                            tuple(updates.keys()),
                                            tuple(updates.values()))
                    if removals:
                        self.ioctx.remove_omap_keys(write_op, tuple(removals))
                    self.ioctx.set_omap(write_op, (REVISION_KEY,),
                                        (str(next_revision),))
                    self.ioctx.operate_write_op(write_op,
                                                WRITER_OBJECT_NAME)
            except (rados.Error, OSError) as e:
                error_code = self._error_code(e)
                if error_code in (-errno.ECANCELED, -errno.EAGAIN):
                    error_code = -errno.EAGAIN
                    self.stale = True
                raise MirrorException(error_code,
                                      'failed to update writer state')
            self.revision = next_revision
            self.stale = False

    def update_instance(self, instance_id: str,
                        record: Optional[Dict[str, Any]]) -> None:
        key = self.instance_key(instance_id)
        if record is None:
            self._mutate({}, [key])
        else:
            self._mutate({key: encode_json(record)}, [])

    def update_directory(self, peer_uuid: str, dir_path: str,
                         record: Optional[Dict[str, Any]],
                         epoch: Optional[int] = None) -> None:
        updates = {}  # type: Dict[str, bytes]
        removals = []  # type: List[str]
        directory_key = self.directory_key(peer_uuid, dir_path)
        if record is None:
            removals.append(directory_key)
        else:
            updates[directory_key] = encode_json(record)
        if epoch is not None:
            if epoch < 0 or epoch > MAX_EPOCH:
                raise MirrorException(-errno.EINVAL,
                                      'assignment epoch is out of range')
            updates[self.epoch_key(peer_uuid, dir_path)] = \
                str(epoch).encode('ascii')
        self._mutate(updates, removals)
