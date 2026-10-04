import base64
import copy
import errno
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import rados

from mgr_module import NotifyType
from mgr_util import open_filesystem

from .exception import MirrorException
from .snapshot_mirror import FSSnapshotMirror
from .utils import AsyncOpTracker, Finisher, connect_to_filesystem, \
    disconnect_from_filesystem
from .writer_notify import WriterInstanceWatcher, WriterNotifier
from .writer_state import MAX_EPOCH, SCHEMA_VERSION, WRITER_OBJECT_NAME, \
    WriterState


log = logging.getLogger(__name__)


class PeerWriterPolicy:
    """Destination-directory placement for one filesystem.

    A committed assignment is never moved merely because its writer stops
    responding. The fencing interface is owned by the MDS follow-up work; until
    it can prove that the exact destination client incarnation is fenced, this
    policy leaves such assignments unavailable.
    """

    RETRY_INTERVAL = 5
    SHUFFLE_INTERVAL = 300
    REQUIRED_FEATURES = frozenset(('assignment_epoch',
                                   'quiesced_release'))

    class DirectoryBalancer:
        def __init__(self):
            self.lock = threading.RLock()
            self.owners = {}  # type: Dict[Tuple[str, str, str], Tuple[str, str]]
            self.peer_counts = {}  # type: Dict[Tuple[str, str, str], int]
            self.fs_counts = {}  # type: Dict[Tuple[str, str], int]
            self.total_counts = {}  # type: Dict[str, int]

        def register(self, filesystem: str, peer_uuid: str, dir_path: str,
                     instance_id: str, daemon_id: str) -> None:
            with self.lock:
                if not daemon_id:
                    return
                key = (filesystem, peer_uuid, dir_path)
                old = self.owners.get(key)
                if old == (instance_id, daemon_id):
                    return
                if old is not None:
                    self._dec(filesystem, peer_uuid, old[1])
                self.owners[key] = (instance_id, daemon_id)
                self._inc(filesystem, peer_uuid, daemon_id)

        def unregister(self, filesystem: str, peer_uuid: str,
                       dir_path: str) -> None:
            with self.lock:
                key = (filesystem, peer_uuid, dir_path)
                old = self.owners.pop(key, None)
                if old is not None:
                    self._dec(filesystem, peer_uuid, old[1])

        @staticmethod
        def _inc_count(counts, key):
            counts[key] = counts.get(key, 0) + 1

        @staticmethod
        def _dec_count(counts, key):
            count = counts.get(key, 0)
            if count <= 1:
                counts.pop(key, None)
            else:
                counts[key] = count - 1

        def _inc(self, filesystem: str, peer_uuid: str,
                 daemon_id: str) -> None:
            self._inc_count(self.peer_counts,
                            (filesystem, peer_uuid, daemon_id))
            self._inc_count(self.fs_counts, (filesystem, daemon_id))
            self._inc_count(self.total_counts, daemon_id)

        def _dec(self, filesystem: str, peer_uuid: str,
                 daemon_id: str) -> None:
            self._dec_count(self.peer_counts,
                            (filesystem, peer_uuid, daemon_id))
            self._dec_count(self.fs_counts, (filesystem, daemon_id))
            self._dec_count(self.total_counts, daemon_id)

        def choose(self, filesystem: str, peer_uuid: str,
                   candidates: Dict[str, Dict[str, Any]]) -> Optional[str]:
            with self.lock:
                best = None
                best_weight = None
                for instance_id in sorted(candidates):
                    daemon_id = candidates[instance_id]['daemon_id']
                    weight = (
                        self.peer_counts.get(
                            (filesystem, peer_uuid, daemon_id), 0),
                        self.fs_counts.get((filesystem, daemon_id), 0),
                        self.total_counts.get(daemon_id, 0),
                        daemon_id,
                        instance_id,
                    )
                    if best_weight is None or weight < best_weight:
                        best_weight = weight
                        best = instance_id
                return best

    def __init__(self, mgr, ioctx, filesystem: str, state: WriterState,
                 directory_balancer: Optional['PeerWriterPolicy.DirectoryBalancer'] = None):
        self.mgr = mgr
        self.ioctx = ioctx
        self.filesystem = filesystem
        self.state = state
        self.lock = threading.Lock()
        self.stopping = False
        self.persisted_instances = {}  # type: Dict[str, Dict[str, Any]]
        self.live_instances = {}  # type: Dict[str, Dict[str, Any]]
        self.directories = {}  # type: Dict[Tuple[str, str], Dict[str, Any]]
        self.epochs = {}  # type: Dict[Tuple[str, str], int]
        self.operation_status = {}  # type: Dict[Tuple[str, str], Dict[str, Any]]
        self.notifier = WriterNotifier(ioctx)
        self.directory_balancer = directory_balancer or \
            PeerWriterPolicy.DirectoryBalancer()
        self.op_tracker = AsyncOpTracker()
        self.finisher = None  # type: Optional[Finisher]
        self.watcher = None  # type: Optional[WriterInstanceWatcher]
        self.retry_task = None  # type: Optional[threading.Timer]
        self.recovery_deadline = time.time() + \
            WriterInstanceWatcher.INSTANCE_TIMEOUT

    def init(self) -> None:
        instances, directories, epochs = self.state.load()
        with self.lock:
            self.persisted_instances = instances
            self.directories = directories
            self.epochs = epochs
            for key, record in list(directories.items()):
                self._register_directory_locked(key, record)
                if record['purging'] and not record['instance_id']:
                    self.state.update_directory(key[0], key[1], None,
                                                epochs.get(key, 0))
                    self._unregister_directory_locked(key)
                    self.directories.pop(key)
                    continue
                if record['instance_id']:
                    self.operation_status[key] = {
                        'state': 'discovering',
                        'reason': 'waiting for the assigned writer incarnation',
                    }
                else:
                    self.operation_status[key] = {
                        'state': 'unassigned',
                        'reason': 'no compatible writer is available',
                    }
            # Persisted instance records are recovery evidence, not liveness.
            self.finisher = Finisher()
            try:
                self.watcher = WriterInstanceWatcher(self.ioctx,
                                                     self._queue_instances)
                self._schedule_retry_locked()
            except Exception:
                if self.watcher:
                    self.watcher.stop()
                    self.watcher = None
                self.finisher.stop()
                self.finisher = None
                raise

    def shutdown(self) -> None:
        with self.lock:
            self.stopping = True
            watcher = self.watcher
            self.watcher = None
            retry_task = self.retry_task
            self.retry_task = None
        if retry_task:
            retry_task.cancel()
            retry_task.join()
        if watcher:
            watcher.stop()
        self.op_tracker.wait_for_ops()
        if self.finisher:
            self.finisher.stop()
        self.ioctx.close()

    def _schedule_retry_locked(self) -> None:
        if self.stopping:
            return
        assert self.retry_task is None
        self.retry_task = threading.Timer(self.RETRY_INTERVAL, self._retry)
        self.retry_task.daemon = True
        self.retry_task.start()

    def _retry(self) -> None:
        notifications = []
        expired = {}
        with self.lock:
            self.retry_task = None
            if self.stopping:
                return
            try:
                if self.state.stale:
                    self._reload_state_locked()
                if time.time() >= self.recovery_deadline:
                    for instance_id in list(self.persisted_instances):
                        if instance_id in self.live_instances:
                            continue
                        try:
                            self.state.update_instance(instance_id, None)
                            self.persisted_instances.pop(instance_id, None)
                        except MirrorException as e:
                            log.error('failed to remove stale writer %s: %s',
                                      instance_id, e.args[1])
                    missing = {record['instance_id']
                               for key, record in self.directories.items()
                               if record['instance_id'] and
                               record['instance_id'] not in self.live_instances and
                               self.operation_status.get(key, {}).get('state') ==
                               'discovering'}
                    for instance_id in sorted(missing):
                        expired[instance_id] = self._mark_instance_expired_locked(
                            instance_id, 'persisted writer was not rediscovered; '
                            'destination client fencing is required')
                for key, record in self.directories.items():
                    status = self.operation_status.get(key, {})
                    if status.get('state') == 'compatibility_pending':
                        instance = self.live_instances.get(
                            record['instance_id'])
                        if instance is None or not self._same_incarnation(
                                instance, record):
                            continue
                        if not self._supports_release(instance):
                            self.operation_status[key] = {
                                'state': 'fencing',
                                'reason': 'writer cannot confirm a quiesced '
                                          'release; exact client fencing is '
                                          'required',
                            }
                            continue
                        updated = dict(record)
                        updated['reassigning'] = True
                        try:
                            self.state.update_directory(
                                key[0], key[1], updated,
                                self.epochs.get(key, 0))
                        except MirrorException as e:
                            self.operation_status[key]['reason'] = \
                                f'failed to persist compatibility release: ' \
                                f'{e.args[0]}'
                            continue
                        self.directories[key] = updated
                        message = self._message('release', key, updated)
                        self.operation_status[key] = {
                            'state': 'releasing',
                            'operation_id': message['operation_id'],
                            'reason': 'writer is no longer compatible',
                        }
                        notifications.append((record['instance_id'], key,
                                              message))
                        continue
                    retry_release = status.get('state') == 'releasing' and \
                        not status.get('operation_id')
                    if (status.get('state') != 'unavailable' and not retry_release) or \
                       not record['instance_id']:
                        continue
                    instance = self.live_instances.get(record['instance_id'])
                    if instance is None or not self._same_incarnation(
                            instance, record):
                        continue
                    mode = 'release' if retry_release or record['purging'] or \
                        record.get('reassigning', False) else 'acquire'
                    if mode == 'release' and \
                       not self._supports_release(instance):
                        self.operation_status[key] = {
                            'state': 'fencing',
                            'reason': 'writer cannot confirm a quiesced '
                                      'release; exact client fencing is '
                                      'required',
                        }
                        continue
                    message = self._message(mode, key, record)
                    self.operation_status[key] = {
                        'state': 'releasing' if mode == 'release'
                        else 'acquiring',
                        'operation_id': message['operation_id'],
                    }
                    notifications.append((record['instance_id'], key,
                                          message))
                for key, record in list(self.directories.items()):
                    if not record['purging'] or record['instance_id']:
                        continue
                    try:
                        self.state.update_directory(
                            key[0], key[1], None,
                            self.epochs.get(key, 0))
                    except MirrorException as e:
                        self.operation_status[key] = {
                            'state': 'unavailable',
                            'reason': 'failed to finish directory removal: '
                                      f'{e.args[0]}',
                        }
                        continue
                    self.directories.pop(key, None)
                    self.operation_status.pop(key, None)
                notifications.extend(self._assign_unassigned_locked())
                notifications.extend(self._plan_rebalance_locked())
            except MirrorException as e:
                log.error('failed to retry PeerWriter policy for %s: %s',
                          self.filesystem, e.args[1])
            finally:
                self._schedule_retry_locked()
        for instance_id, assignments in expired.items():
            self._fence_instance(instance_id, assignments)
        for notification in notifications:
            self._notify(*notification)

    def _mark_instance_expired_locked(
            self, instance_id: str, reason: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
        assignments = {}
        for key, directory in self.directories.items():
            if directory['instance_id'] != instance_id:
                continue
            # Invalidate outstanding release/acquire completions. Liveness
            # expiry must not authorize a late callback to change ownership.
            self.operation_status[key] = {'state': 'fencing', 'reason': reason}
            assignments[key] = copy.deepcopy(directory)
        return assignments

    def _fence_instance(self, instance_id: str,
                        assignments: Dict[Tuple[str, str], Dict[str, Any]]) -> None:
        """Hook for fencing an expired writer; called outside the policy lock.

        The MDS follow-up must enqueue fencing of the captured destination
        client incarnations, not wait for a daemon release acknowledgement.
        It must confirm eviction, blocklisting and the OSD epoch barrier, and
        revalidate ownership/epochs before finishing removal or reassignment.
        Until that integration exists, leave every assignment pending fencing.
        """
        log.debug('writer %s requires fencing for %s directories; '
                  'MDS fencing is not integrated', instance_id, len(assignments))

    @staticmethod
    def _persistent_instance(record: Dict[str, Any]) -> Dict[str, Any]:
        return {
            'version': SCHEMA_VERSION,
            'addr': record['addr'],
            'daemon_id': record['daemon_id'],
            'process_incarnation': record['process_incarnation'],
            'destination_client_identity':
                record['destination_client_identity'],
        }

    def _reload_state_locked(self) -> None:
        instances, directories, epochs = self.state.load()
        for key in self.directories.keys() - directories.keys():
            self._unregister_directory_locked(key)
        self.persisted_instances = instances
        self.directories = directories
        self.epochs = epochs
        for key, record in directories.items():
            self._register_directory_locked(key, record)
        self.operation_status.clear()
        for key, record in list(directories.items()):
            if record['purging'] and not record['instance_id']:
                self.state.update_directory(key[0], key[1], None,
                                            epochs.get(key, 0))
                self.directories.pop(key)
                continue
            if not record['instance_id']:
                self.operation_status[key] = {
                    'state': 'unassigned',
                    'reason': 'state reloaded after a concurrent update',
                }
                continue
            instance = self.live_instances.get(record['instance_id'])
            if instance is not None and self._same_incarnation(instance,
                                                               record):
                if not self._is_compatible(instance):
                    if self._supports_release(instance):
                        self.operation_status[key] = {
                            'state': 'compatibility_pending',
                            'reason': 'state reloaded; writer compatibility '
                                      'release is pending',
                        }
                    else:
                        self.operation_status[key] = {
                            'state': 'fencing',
                            'reason': 'writer cannot confirm a quiesced '
                                      'release; exact client fencing is '
                                      'required',
                        }
                else:
                    self.operation_status[key] = {
                        'state': 'unavailable',
                        'reason': 'state reloaded; writer must reacquire it',
                    }
            else:
                self.operation_status[key] = {
                    'state': 'discovering',
                    'reason': 'state reloaded; waiting for the assigned '
                              'writer incarnation',
                }

    @staticmethod
    def _same_incarnation(instance: Dict[str, Any],
                          directory: Dict[str, Any]) -> bool:
        return instance.get('process_incarnation') == \
            directory.get('process_incarnation') and \
            instance.get('destination_client_identity') == \
            directory.get('destination_client_identity')

    def _is_compatible(self, record: Dict[str, Any]) -> bool:
        return record.get('protocol_version') == 1 and \
            self.REQUIRED_FEATURES.issubset(record.get('features', [])) and \
            self.filesystem in record.get('filesystems', [])

    @staticmethod
    def _supports_release(record: Dict[str, Any]) -> bool:
        return record.get('protocol_version') == 1 and \
            PeerWriterPolicy.REQUIRED_FEATURES.issubset(
                record.get('features', []))

    def _register_directory_locked(self, key: Tuple[str, str],
                                   record: Dict[str, Any]) -> None:
        instance_id = record.get('instance_id')
        if not instance_id:
            self.directory_balancer.unregister(self.filesystem, key[0], key[1])
            return
        instance = self.live_instances.get(instance_id) or \
            self.persisted_instances.get(instance_id)
        daemon_id = (instance or {}).get('daemon_id', instance_id)
        self.directory_balancer.register(self.filesystem, key[0], key[1],
                                         instance_id, daemon_id)

    def _unregister_directory_locked(self, key: Tuple[str, str]) -> None:
        self.directory_balancer.unregister(self.filesystem, key[0], key[1])

    def _choose_instance_locked(self, peer_uuid: str) -> Optional[str]:
        candidates = {instance_id: record for instance_id, record
                      in self.live_instances.items()
                      if self._is_compatible(record)}
        if not candidates:
            return None
        with self.directory_balancer.lock:
            return self.directory_balancer.choose(self.filesystem, peer_uuid,
                                                  candidates)

    @staticmethod
    def _message(mode: str, key: Tuple[str, str],
                 record: Dict[str, Any]) -> Dict[str, Any]:
        return {
            'version': 1,
            'operation_id': str(uuid.uuid4()),
            'mode': mode,
            'instance_id': record['instance_id'],
            'peer_uuid': key[0],
            'path': key[1],
            'assignment_epoch': record['assignment_epoch'],
            'process_incarnation': record['process_incarnation'],
        }

    def _notify(self, instance_id: str, key: Tuple[str, str],
                message: Dict[str, Any]) -> None:
        with self.lock:
            if self.stopping or self.operation_status.get(key, {}).get('operation_id') != \
                    message['operation_id']:
                return
            self.op_tracker.start_async_op()

        def on_finish(result):
            assert self.finisher is not None
            self.finisher.queue(
                self._handle_notify_complete,
                [key, message['operation_id'], message['mode'], result])

        try:
            self.notifier.notify(instance_id, message, on_finish)
        except Exception:
            log.exception('failed to submit writer notification')
            on_finish(-errno.EIO)

    def _handle_notify_complete(self, key: Tuple[str, str],
                                operation_id: str, mode: str,
                                result: int) -> None:
        try:
            if mode == 'release':
                self._handle_release(key, operation_id, result)
            else:
                self._handle_acquire(key, operation_id, result)
        finally:
            self.op_tracker.finish_async_op()

    def _queue_instances(self, added: Dict[str, Dict[str, Any]],
                         removed: Dict[str, Dict[str, Any]]) -> None:
        assert self.finisher is not None
        self.finisher.queue(self.update_instances, [added, removed])

    def _assign_unassigned_locked(self) -> List[Tuple[str, Tuple[str, str],
                                                      Dict[str, Any]]]:
        notifications = []
        # Keep each filesystem's placement batch contiguous. Interleaving
        # backlogs changes the peer/filesystem-first greedy decisions even
        # when individual choose/persist/register operations are atomic.
        with self.directory_balancer.lock:
            for key in sorted(self.directories):
                record = self.directories[key]
                if record['instance_id'] or record['purging']:
                    continue
                instance_id = self._choose_instance_locked(key[0])
                if instance_id is None:
                    self.operation_status[key] = {
                        'state': 'unassigned',
                        'reason': 'no compatible writer is available',
                    }
                    continue
                instance = self.live_instances[instance_id]
                old_epoch = self.epochs.get(key, 0)
                if old_epoch >= MAX_EPOCH:
                    self.operation_status[key] = {
                        'state': 'unavailable',
                        'reason': 'assignment epoch is exhausted',
                    }
                    continue
                epoch = old_epoch + 1
                updated = {
                    'version': SCHEMA_VERSION,
                    'instance_id': instance_id,
                    'assignment_epoch': epoch,
                    'last_shuffled': time.time(),
                    'purging': False,
                    'reassigning': False,
                    'process_incarnation': instance['process_incarnation'],
                    'destination_client_identity':
                        instance['destination_client_identity'],
                }
                try:
                    self.state.update_directory(key[0], key[1], updated,
                                                epoch)
                except MirrorException as e:
                    self.operation_status[key] = {
                        'state': 'unavailable',
                        'reason': 'failed to persist assignment: '
                                  f'{e.args[0]}',
                    }
                    continue
                self.directories[key] = updated
                self.epochs[key] = epoch
                self._register_directory_locked(key, updated)
                message = self._message('acquire', key, updated)
                self.operation_status[key] = {
                    'state': 'acquiring',
                    'operation_id': message['operation_id'],
                }
                notifications.append((instance_id, key, message))
        return notifications

    def _plan_rebalance_locked(
            self) -> List[Tuple[str, Tuple[str, str], Dict[str, Any]]]:
        compatible = sorted(instance_id for instance_id, record
                            in self.live_instances.items()
                            if self._is_compatible(record))
        if len(compatible) < 2:
            return []
        # A release has no replacement owner until it completes. Do not plan
        # another batch against counts that omit these in-flight movements.
        if any(record.get('reassigning', False)
               for record in self.directories.values()):
            return []
        counts = {instance_id: 0 for instance_id in compatible}
        candidates = {instance_id: [] for instance_id in compatible}
        now = time.time()
        for key, record in self.directories.items():
            instance_id = record['instance_id']
            if instance_id not in counts or record['purging'] or \
               record.get('reassigning', False):
                continue
            counts[instance_id] += 1
            if now - record['last_shuffled'] >= self.SHUFFLE_INTERVAL and \
               self.operation_status.get(key, {}).get('state') == 'assigned':
                candidates[instance_id].append(key)
        notifications = []
        while counts:
            high = max(counts, key=lambda item: (counts[item], item))
            low = min(counts, key=lambda item: (counts[item], item))
            if counts[high] - counts[low] <= 1 or not candidates[high]:
                break
            key = sorted(candidates[high],
                         key=lambda item:
                         self.directories[item]['last_shuffled'])[0]
            candidates[high].remove(key)
            record = dict(self.directories[key])
            record['reassigning'] = True
            try:
                self.state.update_directory(key[0], key[1], record,
                                            self.epochs.get(key, 0))
            except MirrorException as e:
                log.error('failed to persist PeerWriter rebalance for %s: %s',
                          key, e.args[1])
                break
            self.directories[key] = record
            message = self._message('release', key, record)
            self.operation_status[key] = {
                'state': 'releasing',
                'operation_id': message['operation_id'],
                'reason': 'rebalancing to a less loaded writer',
            }
            notifications.append((high, key, message))
            counts[high] -= 1
            counts[low] += 1
        return notifications

    def update_instances(self, added: Dict[str, Dict[str, Any]],
                          removed: Dict[str, Dict[str, Any]]) -> None:
        notifications = []
        reacquire = []
        expired = {}
        with self.lock:
            if self.stopping:
                return
            for instance_id, record in sorted(added.items()):
                previous_live = self.live_instances.get(instance_id)
                persistent = self._persistent_instance(record)
                if self.persisted_instances.get(instance_id) != persistent:
                    try:
                        self.state.update_instance(instance_id, persistent)
                    except MirrorException as e:
                        log.error('failed to persist writer %s: %s',
                                  instance_id, e.args[1])
                        continue
                    self.persisted_instances[instance_id] = persistent
                self.live_instances[instance_id] = dict(record)
                for key, directory in self.directories.items():
                    if directory['instance_id'] != instance_id:
                        continue
                    status = self.operation_status.get(key, {})
                    if status.get('state') == 'releasing' and \
                       self._same_incarnation(record, directory):
                        # Discovery refreshes must not replace an in-flight
                        # release operation; the policy timer retries failures.
                        continue
                    if previous_live == record and \
                       status.get('state') not in ('discovering',
                                                   'unavailable',
                                                   'fencing',
                                                   'compatibility_pending'):
                        continue
                    if not self._same_incarnation(record, directory):
                        self.operation_status[key] = {
                            'state': 'fencing',
                            'reason': 'assigned writer incarnation changed; '
                                      'exact client fencing is required',
                        }
                        continue
                    if previous_live is None and \
                       status.get('state') == 'discovering' and \
                       self._is_compatible(record) and \
                       not directory['purging'] and \
                       not directory.get('reassigning', False):
                        self.operation_status[key] = {'state': 'assigned'}
                        self._register_directory_locked(key, directory)
                        continue
                    if directory['purging'] or \
                       directory.get('reassigning', False):
                        if not self._supports_release(record):
                            self.operation_status[key] = {
                                'state': 'fencing',
                                'reason': 'writer cannot confirm a quiesced '
                                          'release; exact client fencing is '
                                          'required',
                            }
                            continue
                        message = self._message('release', key, directory)
                        self.operation_status[key] = {
                            'state': 'releasing',
                            'operation_id': message['operation_id'],
                        }
                    elif not self._is_compatible(record):
                        if not self._supports_release(record):
                            self.operation_status[key] = {
                                'state': 'fencing',
                                'reason': 'writer lost required protocol '
                                          'features; exact client fencing is '
                                          'required',
                            }
                            continue
                        updated = dict(directory)
                        updated['reassigning'] = True
                        try:
                            self.state.update_directory(
                                key[0], key[1], updated,
                                self.epochs.get(key, 0))
                        except MirrorException as e:
                            self.operation_status[key] = {
                                'state': 'compatibility_pending',
                                'reason': 'failed to persist compatibility '
                                          f'release: {e.args[0]}',
                            }
                            continue
                        self.directories[key] = updated
                        message = self._message('release', key, updated)
                        self.operation_status[key] = {
                            'state': 'releasing',
                            'operation_id': message['operation_id'],
                            'reason': 'writer is no longer compatible',
                        }
                    else:
                        message = self._message('acquire', key, directory)
                        self.operation_status[key] = {
                            'state': 'acquiring',
                            'operation_id': message['operation_id'],
                        }
                    reacquire.append((instance_id, key, message))

            for instance_id, record in sorted(removed.items()):
                current = self.live_instances.get(instance_id)
                if current is None or current.get('process_incarnation') != \
                        record.get('process_incarnation'):
                    continue
                self.live_instances.pop(instance_id, None)
                try:
                    self.state.update_instance(instance_id, None)
                    self.persisted_instances.pop(instance_id, None)
                except MirrorException as e:
                    log.error('failed to remove writer %s: %s',
                              instance_id, e.args[1])
                assignments = self._mark_instance_expired_locked(
                    instance_id, 'writer liveness expired; destination client '
                    'fencing is required')
                if assignments:
                    expired[instance_id] = assignments

            notifications = self._assign_unassigned_locked()
            notifications.extend(self._plan_rebalance_locked())
        for instance_id, assignments in expired.items():
            self._fence_instance(instance_id, assignments)
        for notification in reacquire + notifications:
            self._notify(*notification)

    def _handle_acquire(self, key: Tuple[str, str], operation_id: str,
                        result: int) -> None:
        with self.lock:
            if self.stopping or key not in self.directories:
                return
            status = self.operation_status.get(key, {})
            if status.get('operation_id') != operation_id:
                return
            if result == 0:
                self.operation_status[key] = {'state': 'assigned'}
            else:
                self.operation_status[key] = {
                    'state': 'unavailable',
                    'reason': f'writer acquire failed: {result}',
                }

    def _handle_release(self, key: Tuple[str, str], operation_id: str,
                        result: int) -> None:
        with self.lock:
            if self.stopping or key not in self.directories:
                return
            status = self.operation_status.get(key, {})
            if status.get('operation_id') != operation_id:
                return
            if result < 0:
                self.operation_status[key] = {
                    'state': 'releasing',
                    'reason': f'waiting for a quiesced release acknowledgement; '
                              f'last notification failed: {result}',
                }
                return
            try:
                record = self.directories[key]
                if record['purging']:
                    # The epoch floor is deliberately retained on removal.
                    self.state.update_directory(key[0], key[1], None,
                                                self.epochs.get(key, 0))
                    self._unregister_directory_locked(key)
                else:
                    record = {
                        'version': SCHEMA_VERSION,
                        'instance_id': '',
                        'assignment_epoch': self.epochs.get(key, 0),
                        'last_shuffled': record['last_shuffled'],
                        'purging': False,
                        'reassigning': False,
                    }
                    self.state.update_directory(
                        key[0], key[1], record, self.epochs.get(key, 0))
                    self._unregister_directory_locked(key)
            except MirrorException as e:
                self.operation_status[key] = {
                    'state': 'unavailable',
                    'reason': f'failed to persist writer release: {e.args[0]}',
                }
                return
            if self.directories[key]['purging']:
                self.directories.pop(key, None)
                self.operation_status.pop(key, None)
                return
            self.directories[key] = record
            self.operation_status[key] = {
                'state': 'unassigned',
                'reason': 'waiting for replacement assignment',
            }
            notifications = self._assign_unassigned_locked()
        for notification in notifications:
            self._notify(*notification)

    def references_peer(self, peer_uuid: str) -> bool:
        with self.lock:
            return any(key[0] == peer_uuid for key in self.directories)

    def add_directory(self, peer_uuid: str, dir_path: str) -> None:
        notifications = []
        key = (peer_uuid, dir_path)
        with self.lock:
            if key in self.directories:
                raise MirrorException(-errno.EEXIST,
                                      f'directory {dir_path} is already tracked')
            for tracked_peer, tracked_path in self.directories:
                if dir_path == tracked_path:
                    raise MirrorException(
                        -errno.EEXIST,
                        f'directory {dir_path} is already tracked by peer '
                        f'{tracked_peer}')
                common = os.path.commonpath((dir_path, tracked_path))
                if common in (dir_path, tracked_path):
                    raise MirrorException(
                        -errno.EINVAL,
                        f'{dir_path} conflicts with tracked path '
                        f'{tracked_path} for peer {tracked_peer}')
            epoch = self.epochs.get(key, 0)
            record = {
                'version': SCHEMA_VERSION,
                'instance_id': '',
                'assignment_epoch': epoch,
                'last_shuffled': 0.0,
                'purging': False,
                'reassigning': False,
            }
            self.state.update_directory(peer_uuid, dir_path, record, epoch)
            self.directories[key] = record
            self.epochs[key] = epoch
            self.operation_status[key] = {
                'state': 'unassigned',
                'reason': 'no compatible writer is available',
            }
            notifications = self._assign_unassigned_locked()
        for notification in notifications:
            self._notify(*notification)

    def remove_directory(self, peer_uuid: str, dir_path: str) -> None:
        key = (peer_uuid, dir_path)
        notification = None
        with self.lock:
            record = self.directories.get(key)
            if record is None:
                raise MirrorException(-errno.ENOENT,
                                      f'directory {dir_path} is not tracked')
            if record['purging']:
                raise MirrorException(-errno.EINVAL,
                                      f'directory {dir_path} is under removal')
            updated = dict(record)
            updated['purging'] = True
            self.state.update_directory(peer_uuid, dir_path, updated,
                                        self.epochs.get(key, 0))
            self.directories[key] = updated
            if not updated['instance_id']:
                self.state.update_directory(peer_uuid, dir_path, None,
                                            self.epochs.get(key, 0))
                self._unregister_directory_locked(key)
                self.directories.pop(key)
                self.operation_status.pop(key, None)
                return
            instance = self.live_instances.get(updated['instance_id'])
            if instance is None or not self._same_incarnation(instance,
                                                               updated):
                self.operation_status[key] = {
                    'state': 'fencing',
                    'reason': 'writer is unavailable; exact destination '
                              'client fencing is required before removal',
                }
                return
            if not self._supports_release(instance):
                self.operation_status[key] = {
                    'state': 'fencing',
                    'reason': 'writer cannot confirm a quiesced release; '
                              'exact client fencing is required before '
                              'removal',
                }
                return
            message = self._message('release', key, updated)
            self.operation_status[key] = {
                'state': 'releasing',
                'operation_id': message['operation_id'],
            }
            notification = (updated['instance_id'], key, message)
        if notification:
            self._notify(*notification)

    def list_directories(self) -> List[Dict[str, Any]]:
        with self.lock:
            result = []
            for key in sorted(self.directories):
                record = self.directories[key]
                item = {
                    'peer_uuid': key[0],
                    'path': key[1],
                    'instance_id': record['instance_id'],
                    'assignment_epoch': record['assignment_epoch'],
                    'purging': record['purging'],
                }
                item.update(self.operation_status.get(key, {}))
                result.append(item)
            return result

    def directory_status(self, peer_uuid: str,
                         dir_path: str) -> Dict[str, Any]:
        key = (peer_uuid, dir_path)
        with self.lock:
            record = self.directories.get(key)
            if record is None:
                raise MirrorException(-errno.ENOENT,
                                      f'directory {dir_path} is not tracked')
            result = {
                'peer_uuid': peer_uuid,
                'path': dir_path,
                'instance_id': record['instance_id'],
                'assignment_epoch': record['assignment_epoch'],
                'purging': record['purging'],
            }
            result.update(self.operation_status.get(key, {}))
            return result


class PeerWriterControlPlane:
    PEER_CONFIG_KEY_PREFIX = 'cephfs/mirror/peer_writer'

    def __init__(self, mgr, snapshot_mirror: FSSnapshotMirror):
        self.mgr = mgr
        self.rados = mgr.rados
        self.snapshot_mirror = snapshot_mirror
        self.fs_map = mgr.get('fs_map')
        self.lock = threading.Lock()
        self.policies = {}  # type: Dict[str, PeerWriterPolicy]
        self.peers = {}  # type: Dict[Tuple[str, str], Dict[str, Any]]
        self.directory_balancer = PeerWriterPolicy.DirectoryBalancer()
        self.stopping = False
        self._refresh_policies()

    @staticmethod
    def peer_config_key(filesystem: str, peer_uuid: str) -> str:
        return f'{PeerWriterControlPlane.PEER_CONFIG_KEY_PREFIX}/' \
               f'{filesystem}/{peer_uuid}'

    @staticmethod
    def _canonical_uuid(peer_uuid: str) -> str:
        try:
            return str(uuid.UUID(peer_uuid))
        except (AttributeError, ValueError):
            raise MirrorException(-errno.EINVAL,
                                  f'invalid peer UUID {peer_uuid}')

    @staticmethod
    def _norm_path(dir_path: str) -> str:
        return FSSnapshotMirror.norm_path(dir_path)

    @staticmethod
    def _error_code(error: Exception) -> int:
        error_number = getattr(error, 'errno', None)
        if error_number is None and error.args:
            error_number = error.args[0]
        if not isinstance(error_number, int):
            return -errno.EIO
        return error_number if error_number < 0 else -error_number

    def _filesystem(self, filesystem: str) -> Dict[str, Any]:
        for fs in self.fs_map['filesystems']:
            if fs['mdsmap']['fs_name'] == filesystem:
                return fs
        raise MirrorException(-errno.ENOENT,
                              f'filesystem {filesystem} does not exist')

    def _open_policy(self, filesystem: str,
                     create: bool) -> Optional[PeerWriterPolicy]:
        policy = self.policies.get(filesystem)
        if policy:
            return policy
        fs = self._filesystem(filesystem)
        pool_id = fs['mdsmap']['metadata_pool']
        ioctx = self.rados.open_ioctx2(pool_id)
        state = WriterState(ioctx)
        try:
            if create:
                state.initialize()
            else:
                ioctx.stat(WRITER_OBJECT_NAME)
            policy = PeerWriterPolicy(self.mgr, ioctx, filesystem, state,
                                      self.directory_balancer)
            policy.init()
        except rados.Error as e:
            ioctx.close()
            if not create and self._error_code(e) == -errno.ENOENT:
                return None
            raise
        except Exception:
            ioctx.close()
            raise
        self.policies[filesystem] = policy
        return policy

    def _refresh_policies(self) -> None:
        filesystems = {fs['mdsmap']['fs_name']
                       for fs in self.fs_map['filesystems']}
        for filesystem in list(self.policies):
            if filesystem not in filesystems:
                self.policies.pop(filesystem).shutdown()
        for filesystem in sorted(filesystems):
            try:
                self._open_policy(filesystem, False)
            except (MirrorException, rados.Error) as e:
                log.error('failed to load PeerWriter state for %s: %s',
                          filesystem, e)

    def notify(self, notify_type: NotifyType) -> None:
        if notify_type != NotifyType.fs_map:
            return
        with self.lock:
            if self.stopping:
                return
            self.fs_map = self.mgr.get('fs_map')
            self._refresh_policies()

    def shutdown(self) -> None:
        with self.lock:
            self.stopping = True
            policies = list(self.policies.values())
            self.policies.clear()
        for policy in policies:
            policy.shutdown()

    def _config_get(self, key: str) -> Dict[str, Any]:
        result, output, error = self.mgr.mon_command(
            {'prefix': 'config-key get', 'key': key})
        if result == -errno.ENOENT:
            return {}
        if result < 0:
            raise MirrorException(result,
                                  error or 'failed to read PeerWriter secret')
        try:
            value = json.loads(output)
        except (TypeError, ValueError):
            raise MirrorException(-errno.EUCLEAN,
                                  'invalid PeerWriter secret')
        if not isinstance(value, dict):
            raise MirrorException(-errno.EUCLEAN,
                                  'invalid PeerWriter secret')
        return value

    def _config_set(self, key: str,
                    value: Optional[Dict[str, Any]] = None) -> None:
        if value is None:
            command = {'prefix': 'config-key rm', 'key': key}
        else:
            command = {'prefix': 'config-key set', 'key': key,
                       'val': json.dumps(value, sort_keys=True)}
        result, _, error = self.mgr.mon_command(command)
        if result < 0 and not (value is None and result == -errno.ENOENT):
            raise MirrorException(result,
                                  error or 'failed to update PeerWriter secret')

    @staticmethod
    def _decode_mon_output(output) -> Dict[str, Any]:
        if isinstance(output, bytes):
            output = output.decode('utf-8')
        decoded = json.loads(output)
        if not isinstance(decoded, dict):
            raise ValueError('expected a JSON object')
        return decoded

    def _verify_source(self, destination_fs: str, peer_uuid: str,
                       source_cluster_spec: str, source_fs: str,
                       source_conf: Dict[str, Any]) -> None:
        client_name, cluster_name = FSSnapshotMirror.split_spec(
            source_cluster_spec)
        source_cluster, source_handle = connect_to_filesystem(
            client_name, cluster_name, source_fs, 'PeerWriter source',
            conf_dct=source_conf)
        try:
            source_cluster_id = source_cluster.get_fsid()
            if source_conf.get('fsid') and \
               source_conf['fsid'] != source_cluster_id:
                raise MirrorException(
                    -errno.EINVAL,
                    'FSID mismatch between bootstrap token and source cluster')
            source_fscid = source_handle.get_fscid()
            command = json.dumps({'prefix': 'fs dump', 'format': 'json'})
            result, output, error = source_cluster.mon_command(command, b'')
            if result < 0:
                raise MirrorException(result,
                                      error or 'failed to inspect source peer')
            source_map = self._decode_mon_output(output)
            source_entry = None
            for fs in source_map.get('filesystems', []):
                if fs.get('mdsmap', {}).get('fs_name') == source_fs:
                    source_entry = fs
                    break
            peers = (source_entry or {}).get('mirror_info', {}).get('peers', {})
            source_peer = peers.get(peer_uuid)
            if source_peer is None or \
               source_peer.get('remote', {}).get('fs_name') != destination_fs:
                raise MirrorException(
                    -errno.ENOENT,
                    'source filesystem has no reciprocal peer relationship')

            with open_filesystem(self.snapshot_mirror.local_fs,
                                 destination_fs) as destination_handle:
                mirror_info = FSSnapshotMirror.get_mirror_info(
                    destination_handle)
            if mirror_info['cluster_id'] != source_cluster_id or \
               mirror_info['fs_id'] != source_fscid:
                raise MirrorException(
                    -errno.EINVAL,
                    'destination filesystem belongs to another source')
        finally:
            disconnect_from_filesystem(cluster_name, source_fs,
                                       source_cluster, source_handle)

    def _peer_list(self, filesystem: str) -> Dict[str, Dict[str, Any]]:
        result, output, error = self.mgr.mon_command({
            'prefix': 'fs mirror peer_writer peer_list',
            'fs_name': filesystem,
            'format': 'json',
        })
        if result < 0:
            raise MirrorException(result,
                                  error or 'failed to list PeerWriter peers')
        try:
            peers = self._decode_mon_output(output)
        except (UnicodeDecodeError, ValueError):
            raise MirrorException(-errno.EUCLEAN,
                                  'invalid PeerWriter peer list')
        for peer_uuid, peer in peers.items():
            self.peers[(filesystem, peer_uuid)] = peer
        return peers

    def _require_peer(self, filesystem: str, peer_uuid: str) -> None:
        if peer_uuid not in self._peer_list(filesystem):
            raise MirrorException(-errno.ENOENT,
                                  f'PeerWriter peer {peer_uuid} does not exist')

    @staticmethod
    def _peer_matches(peer: Dict[str, Any], source_cluster_spec: str,
                      source_fs: str, destination_fs: str) -> bool:
        configured_spec = peer.get('source_cluster_spec')
        if configured_spec is None:
            client_name = peer.get('source_client_name')
            cluster_name = peer.get('source_cluster_name')
            if client_name and cluster_name:
                configured_spec = f'{client_name}@{cluster_name}'
        configured_fs = peer.get('source_fs_name',
                                 peer.get('source_filesystem'))
        configured_destination = peer.get('destination_filesystem',
                                          destination_fs)
        return configured_spec == source_cluster_spec and \
            configured_fs == source_fs and \
            configured_destination == destination_fs

    @staticmethod
    def _result(callable_, failure: str):
        try:
            value = callable_()
            return 0, json.dumps(value if value is not None else {},
                                 sort_keys=True), ''
        except MirrorException as e:
            return e.args[0], '', e.args[1]
        except Exception as e:
            log.exception(failure)
            error_code = PeerWriterControlPlane._error_code(e)
            return error_code, '', failure

    def peer_add(self, filesystem: str, peer_uuid: str,
                 source_cluster_spec: str, source_fs: str,
                 source_conf: Dict[str, Any]):
        def add():
            canonical_uuid = self._canonical_uuid(peer_uuid)
            if bool(source_conf.get('mon_host')) != bool(source_conf.get('key')):
                raise MirrorException(
                    -errno.EINVAL,
                    'source_mon_host and cephx_key must be supplied together')
            with self.lock:
                self._filesystem(filesystem)
                config_key = self.peer_config_key(filesystem, canonical_uuid)
                stored_conf = self._config_get(config_key)
                secret_conf = {key: source_conf[key]
                               for key in ('fsid', 'mon_host', 'key')
                               if source_conf.get(key)}
                if stored_conf and secret_conf and stored_conf != secret_conf:
                    raise MirrorException(
                        -errno.EEXIST,
                        'PeerWriter peer has different stored connection data')
                effective_conf = secret_conf or stored_conf
                existing_peer = self._peer_list(filesystem).get(
                    canonical_uuid)
                if existing_peer is not None:
                    if not self._peer_matches(existing_peer,
                                              source_cluster_spec, source_fs,
                                              filesystem):
                        raise MirrorException(
                            -errno.EEXIST,
                            'PeerWriter UUID has different source '
                            'configuration')
                    self._open_policy(filesystem, True)
                    if secret_conf and not stored_conf:
                        self._verify_source(filesystem, canonical_uuid,
                                            source_cluster_spec, source_fs,
                                            secret_conf)
                        self._config_set(config_key, secret_conf)
                    return {}
                self._verify_source(filesystem, canonical_uuid,
                                    source_cluster_spec, source_fs,
                                    effective_conf)
                # The base object must exist before a configured writer can
                # register its discovery watch.
                self._open_policy(filesystem, True)
                wrote_secret = False
                if secret_conf and not stored_conf:
                    self._config_set(config_key, secret_conf)
                    wrote_secret = True
                command = {
                    'prefix': 'fs mirror peer_writer peer_add',
                    'fs_name': filesystem,
                    'uuid': canonical_uuid,
                    'source_cluster_spec': source_cluster_spec,
                    'source_fs_name': source_fs,
                }
                result, _, error = self.mgr.mon_command(command)
                if result < 0:
                    reconciled = False
                    try:
                        peer = self._peer_list(filesystem).get(canonical_uuid)
                        reconciled = peer is not None and self._peer_matches(
                            peer, source_cluster_spec, source_fs, filesystem)
                        if peer is not None and not reconciled:
                            raise MirrorException(
                                -errno.EEXIST,
                                'PeerWriter UUID has different source '
                                'configuration')
                    except MirrorException:
                        # An uncertain add result must not remove credentials
                        # that an active relationship might require.
                        raise MirrorException(
                            result, error or 'failed to add PeerWriter peer')
                    if not reconciled:
                        if wrote_secret:
                            try:
                                self._config_set(config_key)
                            except MirrorException:
                                log.warning(
                                    'failed to roll back PeerWriter secret %s',
                                    config_key)
                        raise MirrorException(
                            result, error or 'failed to add PeerWriter peer')
                self.peers[(filesystem, canonical_uuid)] = {
                    'source_cluster_spec': source_cluster_spec,
                    'source_fs_name': source_fs,
                }
            return {}
        return self._result(add, 'failed to add PeerWriter peer')

    def peer_bootstrap_import(self, filesystem: str, token: str):
        try:
            token_data = json.loads(base64.b64decode(token,
                                                     validate=True).decode('utf-8'))
            required = ('peer_uuid', 'fsid', 'filesystem', 'user',
                        'site_name', 'key', 'mon_host')
            if not isinstance(token_data, dict) or \
               any(not token_data.get(field) for field in required):
                raise ValueError('missing token field')
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
            return -errno.EINVAL, '', 'failed to parse PeerWriter token'
        source_cluster_spec = \
            f"{token_data['user']}@{token_data['site_name']}"
        source_conf = {
            'fsid': token_data['fsid'],
            'key': token_data['key'],
            'mon_host': token_data['mon_host'],
        }
        return self.peer_add(filesystem, token_data['peer_uuid'],
                             source_cluster_spec, token_data['filesystem'],
                             source_conf)

    def peer_remove(self, filesystem: str, peer_uuid: str):
        def remove():
            canonical_uuid = self._canonical_uuid(peer_uuid)
            with self.lock:
                self._filesystem(filesystem)
                policy = self._open_policy(filesystem, False)
                if policy and policy.references_peer(canonical_uuid):
                    raise MirrorException(
                        -errno.EBUSY,
                        'PeerWriter peer still has destination directories')
                command = {
                    'prefix': 'fs mirror peer_writer peer_remove',
                    'fs_name': filesystem,
                    'uuid': canonical_uuid,
                }
                result, _, error = self.mgr.mon_command(command)
                if result < 0 and result != -errno.ENOENT:
                    raise MirrorException(
                        result, error or 'failed to remove PeerWriter peer')
                self._config_set(self.peer_config_key(filesystem,
                                                      canonical_uuid))
                self.peers.pop((filesystem, canonical_uuid), None)
            return {}
        return self._result(remove, 'failed to remove PeerWriter peer')

    def add_directory(self, filesystem: str, peer_uuid: str, dir_path: str):
        def add():
            canonical_uuid = self._canonical_uuid(peer_uuid)
            normalized_path = self._norm_path(dir_path)
            with self.lock:
                self._filesystem(filesystem)
                self._require_peer(filesystem, canonical_uuid)
                policy = self._open_policy(filesystem, True)
                assert policy is not None
                policy.add_directory(canonical_uuid, normalized_path)
            return {}
        return self._result(add, 'failed to add PeerWriter directory')

    def remove_directory(self, filesystem: str, peer_uuid: str,
                         dir_path: str):
        def remove():
            canonical_uuid = self._canonical_uuid(peer_uuid)
            normalized_path = self._norm_path(dir_path)
            with self.lock:
                self._filesystem(filesystem)
                policy = self._open_policy(filesystem, False)
                if policy is None:
                    raise MirrorException(-errno.ENOENT,
                                          'no PeerWriter directories are tracked')
                policy.remove_directory(canonical_uuid, normalized_path)
            return {}
        return self._result(remove, 'failed to remove PeerWriter directory')

    def list_directories(self, filesystem: str):
        def list_():
            with self.lock:
                self._filesystem(filesystem)
                policy = self._open_policy(filesystem, False)
                return policy.list_directories() if policy else []
        return self._result(list_, 'failed to list PeerWriter directories')

    def status(self, filesystem: str, peer_uuid: str, dir_path: str):
        def status_():
            canonical_uuid = self._canonical_uuid(peer_uuid)
            normalized_path = self._norm_path(dir_path)
            with self.lock:
                self._filesystem(filesystem)
                policy = self._open_policy(filesystem, False)
                if policy is None:
                    raise MirrorException(-errno.ENOENT,
                                          'no PeerWriter directories are tracked')
                return policy.directory_status(canonical_uuid,
                                               normalized_path)
        return self._result(status_, 'failed to get PeerWriter status')
