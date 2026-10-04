import os
import copy
import json
import errno
import importlib.util
import logging
import random
import sys
import time
import types

from io import StringIO
from collections import Counter, deque

from tasks.cephfs.cephfs_test_case import CephFSTestCase
from teuthology.exceptions import CommandFailedError
from teuthology.contextutil import safe_while

log = logging.getLogger(__name__)
pw_log = logging.getLogger('tasks.vstart_runner')

mgr_python_path = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', '..', 'src', 'pybind', 'mgr'))


def _load_mirroring_fs_module(name):
    mirroring_path = os.path.join(mgr_python_path, 'mirroring')
    fs_path = os.path.join(mirroring_path, 'fs')
    package_name = '_peer_writer_test_mirroring'
    fs_package_name = f'{package_name}.fs'
    if package_name not in sys.modules:
        module = types.ModuleType(package_name)
        module.__path__ = [mirroring_path]
        sys.modules[package_name] = module
    if fs_package_name not in sys.modules:
        module = types.ModuleType(fs_package_name)
        module.__path__ = [fs_path]
        sys.modules[fs_package_name] = module
    module_name = f'{fs_package_name}.{name}'
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(fs_path, f'{name}.py'))
    if spec is None or spec.loader is None:
        raise ImportError(f'cannot load mirroring.fs.{name}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


PEER_WRITER_TOPOLOGY = {
    'fs0': {'peer00': 7, 'peer01': 5},
    'fs1': {'peer10': 6, 'peer11': 7},
    'fs2': {'peer20': 5, 'peer21': 6},
}

PEER_WRITER_UUIDS = {
    'peer00': '00000000-0000-0000-0000-000000000000',
    'peer01': '00000000-0000-0000-0000-000000000001',
    'peer10': '00000000-0000-0000-0000-000000000010',
    'peer11': '00000000-0000-0000-0000-000000000011',
    'peer20': '00000000-0000-0000-0000-000000000020',
    'peer21': '00000000-0000-0000-0000-000000000021',
}


class _PeerWriterAckGate:
    def __init__(self):
        # Watch callbacks run on native RADOS threads, not gevent greenlets.
        from gevent.monkey import get_original
        allocate_lock = get_original('_thread', 'allocate_lock')
        self.received = allocate_lock()
        self.proceed = allocate_lock()
        self.received.acquire()
        self.proceed.acquire()
        self.message = None
        self.opened = False
        self.timed_out = False

    def hold(self, message):
        if self.message is None:
            self.message = dict(message)
            self.received.release()
        if not self.proceed.acquire(timeout=10):
            self.timed_out = True
            return False
        self.proceed.release()
        return True

    def wait_received(self):
        if not self.received.acquire(timeout=2):
            raise AssertionError('delayed writer notification was not received')
        return self.message

    def allow_ack(self):
        if not self.opened:
            self.opened = True
            self.proceed.release()


class _PeerWriterFakeDaemon:
    def __init__(self, env, daemon_id):
        import rados
        self.env = env
        self.daemon_id = daemon_id
        self.cluster = rados.Rados(conffile=env.conffile)
        self.cluster.connect()
        self.instance_id = str(self.cluster.get_instance_id())
        self.ioctxs = {}
        self.alive = False
        self.acquired = []
        self.released = []
        self.process_incarnation = f'incarnation-{daemon_id}'
        self.destination_client_identity = f'client-{daemon_id}'
        self.discovery_watches = []
        self.command_watches = []
        self.discovery_acks = 0
        self.last_discovery_ack = None
        self.watch_errors = []
        self.callback_logs = deque()
        self.ack_gates = {}
        self.closed = False

    def record(self):
        return {
            'version': 1,
            'addr': f'127.0.0.1:68{self.daemon_id[-1]}',
            'daemon_id': self.daemon_id,
            'process_incarnation': self.process_incarnation,
            'protocol_version': 1,
            'destination_client_identity':
                self.destination_client_identity,
            'filesystems': sorted(self.env.topology),
            'features': ['assignment_epoch', 'quiesced_release'],
        }

    def publish(self, fs_name):
        self.alive = True
        writer_state = _load_mirroring_fs_module('writer_state')
        WRITER_OBJECT_NAME = writer_state.WRITER_OBJECT_NAME
        WRITER_OBJECT_PREFIX = writer_state.WRITER_OBJECT_PREFIX
        ioctx = self.ioctxs.get(fs_name)
        if ioctx is None:
            ioctx = self.cluster.open_ioctx2(self.env.pool_ids[fs_name])
            self.ioctxs[fs_name] = ioctx
        self.command_object = f'{WRITER_OBJECT_PREFIX}.{self.instance_id}'
        ioctx.write_full(self.command_object, b'')
        self.discovery_watches.append(ioctx.watch(
            WRITER_OBJECT_NAME, self._discovery_notify,
            self._watch_error))
        self.command_watches.append(ioctx.watch(
            self.command_object, self._command_notify,
            self._watch_error))
        pw_log.info('PEER_WRITER_TEST daemon %s published watch for %s '
                    'instance %s', self.daemon_id, fs_name,
                    self.instance_id)
        return self.record()

    def stop_heartbeat(self):
        self.alive = False
        pw_log.info('PEER_WRITER_TEST daemon %s stopping discovery heartbeat',
                    self.daemon_id)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.release_delayed_acks()
        errors = []
        for watches in (self.command_watches, self.discovery_watches):
            while watches:
                try:
                    watches.pop().close()
                except Exception as error:
                    errors.append(error)
        self.flush_callback_logs()
        for ioctx in self.ioctxs.values():
            try:
                ioctx.close()
            except Exception as error:
                errors.append(error)
        try:
            self.cluster.shutdown()
        except Exception as error:
            errors.append(error)
        if errors:
            raise errors[0]

    def current_dirs(self):
        return self.env.get_daemon_dirs(self.daemon_id)

    def _discovery_notify(self, notify_id, notifier_id, watch_id, data):
        if not self.alive:
            return ''
        self.discovery_acks += 1
        self.last_discovery_ack = time.monotonic()
        return json.dumps(self.record(), sort_keys=True)

    def _command_notify(self, notify_id, notifier_id, watch_id, data):
        message = json.loads(data.decode('utf-8'))
        directory = self.env.directory_id(message['peer_uuid'],
                                          message['path'])
        gate = self.ack_gates.get((message['mode'], directory))
        if gate is not None and not gate.hold(message):
            self.watch_errors.append((watch_id, 'delayed ACK gate timed out'))
            return ''
        if message['mode'] == 'acquire':
            self.acquired.append(directory)
        else:
            self.released.append(directory)
        # RADOS invokes this on a native thread. Teuthology's gevent-patched
        # logging locks cannot safely be used there; drain logs on the test
        # thread instead so returning the acknowledgment never blocks on them.
        self.callback_logs.append((
            logging.INFO,
            'PEER_WRITER_TEST daemon %s ack %s %s epoch %s',
            (self.daemon_id, message['mode'], directory,
             message['assignment_epoch'])))
        return json.dumps({
            'version': 1,
            'operation_id': message['operation_id'],
            'peer_uuid': message['peer_uuid'],
            'path': message['path'],
            'assignment_epoch': message['assignment_epoch'],
            'process_incarnation': message['process_incarnation'],
            'result': 0,
            'quiesced': True,
        }, sort_keys=True)

    def _watch_error(self, watch_id, error):
        self.watch_errors.append((watch_id, error))
        self.callback_logs.append((
            logging.ERROR,
            'PEER_WRITER_TEST daemon %s watch %s failed: %s',
            (self.daemon_id, watch_id, error)))

    def flush_callback_logs(self):
        while self.callback_logs:
            level, message, args = self.callback_logs.popleft()
            pw_log.log(level, message, *args)

    def delay_ack(self, mode, peer_uuid, path):
        key = (mode, self.env.directory_id(peer_uuid, path))
        assert key not in self.ack_gates
        gate = _PeerWriterAckGate()
        self.ack_gates[key] = gate
        return gate

    def release_delayed_acks(self):
        for gate in self.ack_gates.values():
            gate.allow_ack()
        self.ack_gates.clear()


class _PeerWriterBalancerEnv:
    def __init__(self, testcase, topology=None, daemon_count=4):
        self.WriterState = _load_mirroring_fs_module('writer_state').WriterState
        self.testcase = testcase
        self.topology = topology or PEER_WRITER_TOPOLOGY
        self.daemon_count = daemon_count
        self.cluster = None
        self.ioctxs = {}
        self.pool_ids = {}
        self.conffile = 'ceph.conf' if os.path.exists('ceph.conf') else None
        self.states = {}
        self.daemons = {}
        self.instance_to_daemon = {}
        self.all_dirs = []
        self.closed = False
        try:
            self._connect_rados()
        except Exception:
            try:
                self.close()
            except Exception:
                pw_log.exception(
                    'PEER_WRITER_TEST partial environment cleanup failed')
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        errors = []
        for daemon in self.daemons.values():
            try:
                daemon.close()
            except Exception as error:
                errors.append(error)
        for ioctx in self.ioctxs.values():
            try:
                ioctx.close()
            except Exception as error:
                errors.append(error)
        if self.cluster is not None:
            try:
                self.cluster.shutdown()
            except Exception as error:
                errors.append(error)
        if errors:
            raise errors[0]

    def _connect_rados(self):
        import rados
        self._ensure_filesystems()
        fs_map = json.loads(self.testcase.get_ceph_cmd_stdout(
            'fs', 'dump', '--format=json'))
        filesystems = {
            fs['mdsmap']['fs_name']: fs
            for fs in fs_map['filesystems']
        }
        self.cluster = rados.Rados(conffile=self.conffile)
        self.cluster.connect()
        for fs_name in self.topology:
            fs = filesystems[fs_name]
            pool_id = fs['mdsmap']['metadata_pool']
            self.pool_ids[fs_name] = pool_id
            ioctx = self.cluster.open_ioctx2(pool_id)
            self.ioctxs[fs_name] = ioctx
            self.states[fs_name] = self.WriterState(ioctx)

    def _ensure_filesystems(self):
        if len(self.topology) > 1:
            self.testcase.run_ceph_cmd('fs', 'flag', 'set',
                                       'enable_multiple', 'true',
                                       '--yes-i-really-mean-it')
        existing = {fs['mdsmap']['fs_name'] for fs in json.loads(
            self.testcase.get_ceph_cmd_stdout(
                'fs', 'dump', '--format=json'))['filesystems']}
        for fs_name in self.topology:
            if fs_name in existing:
                continue
            meta_pool = f'{fs_name}_meta'
            data_pool = f'{fs_name}_data'
            self.testcase.run_ceph_cmd('osd', 'pool', 'create', meta_pool)
            self.testcase.run_ceph_cmd('osd', 'pool', 'create', data_pool)
            self.testcase.run_ceph_cmd('fs', 'new', fs_name, meta_pool,
                                       data_pool)

    @staticmethod
    def directory_id(peer_uuid, path):
        return f'{peer_uuid}{path}'

    @staticmethod
    def peer_uuid(peer_label):
        return PEER_WRITER_UUIDS.get(peer_label, peer_label)

    def populate(self):
        pw_log.info('PEER_WRITER_TEST populate start')
        for fs_name, peers in self.topology.items():
            for peer_label in peers:
                peer_uuid = self.peer_uuid(peer_label)
                self.testcase.run_ceph_cmd(
                    'fs', 'mirror', 'peer_writer', 'peer_add', fs_name,
                    peer_uuid, f'client.{peer_uuid}@fake-site',
                    f'source-{fs_name}')
        for idx in range(self.daemon_count):
            daemon = _PeerWriterFakeDaemon(self, f'daemon.{idx}')
            self.daemons[daemon.daemon_id] = daemon
            self.instance_to_daemon[daemon.instance_id] = daemon.daemon_id
        for fs_name, peers in self.topology.items():
            for peer_label, count in peers.items():
                peer_uuid = self.peer_uuid(peer_label)
                for idx in range(count):
                    path = f'/{peer_label}/dir{idx}'
                    self.all_dirs.append(self.directory_id(peer_uuid, path))
                    self.testcase.run_ceph_cmd(
                        'fs', 'snapshot', 'mirror', 'peer_writer', 'add',
                        fs_name, peer_uuid, path)
        # Initialize directory state before registering watches, but do not
        # let discovery place the backlog against a partially published set
        # of daemons. Module changes respawn the mgr; wait for the old process
        # to disappear before installing any discovery watches.
        previous_mgr_gid = json.loads(self.testcase.get_ceph_cmd_stdout(
            'mgr', 'dump', '--format=json'))['active_gid']
        self.testcase.run_ceph_cmd('mgr', 'module', 'disable',
                                   self.testcase.MODULE_NAME)
        self.testcase.mirroring_module_enabled = False

        def module_stopped():
            mgr_map = json.loads(self.testcase.get_ceph_cmd_stdout(
                'mgr', 'dump', '--format=json'))
            return mgr_map['available'] and \
                mgr_map['active_gid'] != previous_mgr_gid

        self.wait_until(module_stopped)
        for daemon in self.daemons.values():
            for fs_name in self.topology:
                daemon.publish(fs_name)
        self.testcase.run_ceph_cmd('mgr', 'module', 'enable',
                                   self.testcase.MODULE_NAME)
        self.testcase.mirroring_module_enabled = True
        self.wait_for_instances()
        self.wait_for_assignments()
        self.log_state('populate complete')

    def restart_mgr_module(self):
        self.log_state('before mgr module restart')
        discovery_acks = {
            daemon_id: daemon.discovery_acks
            for daemon_id, daemon in self.daemons.items()
        }
        self.testcase.run_ceph_cmd('mgr', 'module', 'disable',
                                   self.testcase.MODULE_NAME)
        self.testcase.mirroring_module_enabled = False
        self.testcase.run_ceph_cmd('mgr', 'module', 'enable',
                                   self.testcase.MODULE_NAME)
        self.testcase.mirroring_module_enabled = True
        self.wait_until(lambda: all(
            daemon.discovery_acks > discovery_acks[daemon_id]
            for daemon_id, daemon in self.daemons.items()))
        self.wait_for_assignments(retry_command_timeout=True)
        self.log_state('after mgr module restart')

    def load_state(self, fs_name):
        return self.states[fs_name].load()

    def stop_daemon(self, daemon_id):
        self.log_state(f'before stopping {daemon_id}')
        daemon = self.daemons[daemon_id]
        owned = set(self.get_daemon_dirs(daemon_id))
        daemon.stop_heartbeat()
        started = time.monotonic()

        def expired():
            records = self.directory_records()
            pending = {self.directory_id(item['peer_uuid'], item['path'])
                       for items in records.values() for item in items
                       if item.get('instance_id') == daemon.instance_id and
                       item.get('state') == 'fencing'}
            return pending == owned and all(
                daemon.instance_id not in self.load_state(fs_name)[0]
                for fs_name in self.topology)

        self.wait_until(expired, timeout=45)
        silence = None if daemon.last_discovery_ack is None else \
            time.monotonic() - daemon.last_discovery_ack
        pw_log.info('PEER_WRITER_TEST daemon %s removed after %.3fs; '
                    'discovery silence %.3fs', daemon_id,
                    time.monotonic() - started, silence or 0)
        self.log_state(f'after stopping {daemon_id}')

    def directory_records(self):
        return {fs_name: self.list_directories(fs_name)
                for fs_name in self.topology}

    def owners(self, records=None):
        records = records or self.directory_records()
        result = {}
        for items in records.values():
            for item in items:
                instance_id = item.get('instance_id')
                if instance_id:
                    result[self.directory_id(item['peer_uuid'],
                                             item['path'])] = \
                        self.instance_to_daemon.get(instance_id, instance_id)
        return result

    def _daemon_for_record(self, record):
        instance_id = record['instance_id']
        return self.instance_to_daemon.get(instance_id, instance_id)

    def list_directories(self, fs_name):
        return json.loads(self.testcase.get_ceph_cmd_stdout(
            'fs', 'snapshot', 'mirror', 'peer_writer', 'ls', fs_name,
            '--format=json'))

    def assigned_directory_count(self):
        return len(self.owners())

    def unassigned_directory_count(self):
        return len(self.all_dirs) - self.assigned_directory_count()

    def get_daemon_dirs(self, daemon_id):
        return sorted(directory for directory, owner in self.owners().items()
                      if owner == daemon_id)

    def peer_distribution(self, peer_uuid, daemons=None, records=None):
        peer_uuid = self.peer_uuid(peer_uuid)
        daemons = sorted(daemons or self.daemons)
        counts = Counter()
        prefix = f'{peer_uuid}/'
        for directory, owner in self.owners(records).items():
            if directory.startswith(prefix):
                counts[owner] += 1
        return [counts[daemon] for daemon in daemons]

    def fs_distribution(self, fs_name, daemons=None, records=None):
        daemons = sorted(daemons or self.daemons)
        counts = Counter()
        records = records or self.directory_records()
        for item in records[fs_name]:
            if item.get('instance_id'):
                counts[self.instance_to_daemon.get(item['instance_id'],
                                                   item['instance_id'])] += 1
        return [counts[daemon] for daemon in daemons]

    def total_distribution(self, daemons=None, records=None):
        daemons = sorted(daemons or self.daemons)
        counts = Counter(self.owners(records).values())
        return [counts[daemon] for daemon in daemons]

    def assert_unique_assignment(self, testcase):
        testcase.assertEqual(set(self.owners()), set(self.all_dirs))
        testcase.assertEqual(len(self.owners()), len(self.all_dirs))

    def assert_rados_matches_policy(self, testcase):
        rados_owners, rados_epochs = self.rados_owners_and_epochs(testcase)
        testcase.assertEqual(rados_owners, self.owners())
        testcase.assertEqual(set(rados_epochs), set(self.owners()))

    def rados_owners_and_epochs(self, testcase):
        rados_owners = {}
        rados_epochs = {}
        for fs_name in self.topology:
            _, directories, epochs = self.load_state(fs_name)
            mgr_keys = {(item['peer_uuid'], item['path'])
                        for item in self.list_directories(fs_name)}
            testcase.assertEqual(set(directories), mgr_keys)
            testcase.assertEqual(set(epochs), set(directories))
            for key, record in directories.items():
                testcase.assertEqual(record['assignment_epoch'], epochs[key])
                if record['instance_id']:
                    rados_owners[self.directory_id(key[0], key[1])] = \
                        self._daemon_for_record(record)
                    rados_epochs[self.directory_id(key[0], key[1])] = \
                        epochs[key]
        return rados_owners, rados_epochs

    def rados_peer_distribution(self, testcase, peer_uuid, daemons=None):
        peer_uuid = self.peer_uuid(peer_uuid)
        daemons = sorted(daemons or self.daemons)
        rados_owners, _ = self.rados_owners_and_epochs(testcase)
        counts = Counter()
        prefix = f'{peer_uuid}/'
        for directory, owner in rados_owners.items():
            if directory.startswith(prefix):
                counts[owner] += 1
        return [counts[daemon] for daemon in daemons]

    def rados_fs_distribution(self, testcase, fs_name, daemons=None):
        daemons = sorted(daemons or self.daemons)
        counts = Counter()
        _, directories, _ = self.load_state(fs_name)
        for key, record in directories.items():
            if record['instance_id']:
                counts[self._daemon_for_record(record)] += 1
        return [counts[daemon] for daemon in daemons]

    def rados_total_distribution(self, testcase, daemons=None):
        daemons = sorted(daemons or self.daemons)
        rados_owners, _ = self.rados_owners_and_epochs(testcase)
        counts = Counter(rados_owners.values())
        return [counts[daemon] for daemon in daemons]

    def _all_assigned(self, records):
        items = [item for fs_items in records.values() for item in fs_items]
        return len(items) == len(self.all_dirs) and all(
            item.get('instance_id') and item.get('state') == 'assigned'
            for item in items)

    def wait_for_instances(self):
        expected = set(self.instance_to_daemon)
        MirrorException = _load_mirroring_fs_module('exception').MirrorException

        def ready():
            try:
                return all(set(self.load_state(fs_name)[0]) == expected
                           for fs_name in self.topology)
            except MirrorException as error:
                if error.args[0] != -errno.EAGAIN:
                    raise
                pw_log.info('PEER_WRITER_TEST writer state changed during '
                            'discovery readiness check; retrying')
                return False

        pw_log.info('PEER_WRITER_TEST waiting for %s instances',
                    len(expected))
        self.wait_until(ready)
        pw_log.info('PEER_WRITER_TEST observed %s instances', len(expected))

    def wait_for_assignments(self, retry_command_timeout=False):
        def ready():
            try:
                return self._all_assigned(self.directory_records())
            except CommandFailedError as error:
                if not retry_command_timeout or \
                   error.exitstatus != errno.ETIMEDOUT:
                    raise
                pw_log.info('PEER_WRITER_TEST mgr module is not ready; '
                            'retrying directory query')
                return False

        expected = len(self.all_dirs)
        pw_log.info('PEER_WRITER_TEST waiting for %s assigned directories',
                    expected)
        self.wait_until(ready)
        pw_log.info('PEER_WRITER_TEST observed %s assigned directories',
                    expected)

    def wait_until(self, predicate, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for daemon in self.daemons.values():
                daemon.flush_callback_logs()
            watch_errors = {
                daemon_id: daemon.watch_errors
                for daemon_id, daemon in self.daemons.items()
                if daemon.watch_errors
            }
            if watch_errors:
                raise AssertionError(f'writer watch errors: {watch_errors}')
            if predicate():
                return
            time.sleep(5)
        raise AssertionError(self.debug_state())

    def debug_state(self):
        return '\n'.join([
            f'alive daemons: {[d for d, v in self.daemons.items() if v.alive]}',
            f'owners: {self.owners()}',
            f'unassigned dirs: {self.unassigned_directory_count()}',
            f'per-peer: {self._debug_peer_counts()}',
            f'per-fs: {self._debug_fs_counts()}',
            f'totals: {dict(zip(sorted(self.daemons), self.total_distribution()))}',
        ])

    def log_state(self, label):
        records = self.directory_records()
        pw_log.info('PEER_WRITER_TEST %s totals=%s per_fs=%s per_peer=%s',
                    label,
                    dict(zip(sorted(self.daemons),
                             self.total_distribution(records=records))),
                    self._debug_fs_counts(records),
                    self._debug_peer_counts(records))

    def _debug_peer_counts(self, records=None):
        return {peer: self.peer_distribution(peer, records=records)
                for peers in self.topology.values()
                for peer in peers}

    def _debug_fs_counts(self, records=None):
        return {fs_name: self.fs_distribution(fs_name, records=records)
                for fs_name in self.topology}


class TestMirroring(CephFSTestCase):
    MDSS_REQUIRED = 5
    CLIENTS_REQUIRED = 2
    REQUIRE_BACKUP_FILESYSTEM = True

    MODULE_NAME = "mirroring"

    PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR = "cephfs_mirror"
    PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_FS = "cephfs_mirror_mirrored_filesystems"
    PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER = "cephfs_mirror_peers"

    def setUp(self):
        super(TestMirroring, self).setUp()
        self.primary_fs_name = self.fs.name
        self.primary_fs_id = self.fs.id
        self.secondary_fs_name = self.backup_fs.name
        self.secondary_fs_id = self.backup_fs.id
        self.enable_mirroring_module()

    def tearDown(self):
        self.disable_mirroring_module()
        super(TestMirroring, self).tearDown()

    def enable_mirroring_module(self):
        self.run_ceph_cmd("mgr", "module", "enable", TestMirroring.MODULE_NAME)

    def disable_mirroring_module(self):
        self.run_ceph_cmd("mgr", "module", "disable", TestMirroring.MODULE_NAME)

    def enable_mirroring(self, fs_name, fs_id):
        res = self.mirror_daemon_command(f'counter dump for fs: {fs_name}', 'counter', 'dump')
        vbefore = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR][0]

        self.run_ceph_cmd("fs", "snapshot", "mirror", "enable", fs_name)
        time.sleep(10)
        # verify via asok
        res = self.mirror_daemon_command(f'mirror status for fs: {fs_name}',
                                         'fs', 'mirror', 'status', f'{fs_name}@{fs_id}')
        self.assertTrue(res['peers'] == {})
        self.assertTrue(res['snap_dirs']['dir_count'] == 0)

        # verify labelled perf counter
        res = self.mirror_daemon_command(f'counter dump for fs: {fs_name}', 'counter', 'dump')
        self.assertEqual(res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_FS][0]["labels"]["filesystem"],
                         fs_name)
        vafter = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR][0]

        self.assertGreater(vafter["counters"]["mirrored_filesystems"],
                           vbefore["counters"]["mirrored_filesystems"])

    def disable_mirroring(self, fs_name, fs_id):
        res = self.mirror_daemon_command(f'counter dump for fs: {fs_name}', 'counter', 'dump')
        vbefore = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR][0]

        self.run_ceph_cmd("fs", "snapshot", "mirror", "disable", fs_name)
        time.sleep(10)
        # verify via asok
        try:
            self.mirror_daemon_command(f'mirror status for fs: {fs_name}',
                                       'fs', 'mirror', 'status', f'{fs_name}@{fs_id}')
        except CommandFailedError:
            pass
        else:
            raise RuntimeError('expected admin socket to be unavailable')

        # verify labelled perf counter
        res = self.mirror_daemon_command(f'counter dump for fs: {fs_name}', 'counter', 'dump')
        vafter = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR][0]

        self.assertLess(vafter["counters"]["mirrored_filesystems"],
                        vbefore["counters"]["mirrored_filesystems"])

    def verify_peer_added(self, fs_name, fs_id, peer_spec, remote_fs_name=None):
        # verify via asok
        res = self.mirror_daemon_command(f'mirror status for fs: {fs_name}',
                                         'fs', 'mirror', 'status', f'{fs_name}@{fs_id}')
        peer_uuid = self.get_peer_uuid(peer_spec)
        self.assertTrue(peer_uuid in res['peers'])
        client_name = res['peers'][peer_uuid]['remote']['client_name']
        cluster_name = res['peers'][peer_uuid]['remote']['cluster_name']
        self.assertTrue(peer_spec == f'{client_name}@{cluster_name}')
        if remote_fs_name:
            self.assertTrue(self.secondary_fs_name == res['peers'][peer_uuid]['remote']['fs_name'])
        else:
            self.assertTrue(self.fs_name == res['peers'][peer_uuid]['remote']['fs_name'])

    def peer_add(self, fs_name, fs_id, peer_spec, remote_fs_name=None, check_perf_counter=True):
        if check_perf_counter:
            res = self.mirror_daemon_command(f'counter dump for fs: {fs_name}', 'counter', 'dump')
            vbefore = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_FS][0]

        if remote_fs_name:
            self.run_ceph_cmd("fs", "snapshot", "mirror", "peer_add", fs_name, peer_spec, remote_fs_name)
        else:
            self.run_ceph_cmd("fs", "snapshot", "mirror", "peer_add", fs_name, peer_spec)
        time.sleep(10)
        self.verify_peer_added(fs_name, fs_id, peer_spec, remote_fs_name)

        if check_perf_counter:
            res = self.mirror_daemon_command(f'counter dump for fs: {fs_name}', 'counter', 'dump')
            vafter = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_FS][0]
            self.assertGreater(vafter["counters"]["mirroring_peers"], vbefore["counters"]["mirroring_peers"])

    def peer_remove(self, fs_name, fs_id, peer_spec):
        res = self.mirror_daemon_command(f'counter dump for fs: {fs_name}', 'counter', 'dump')
        vbefore = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_FS][0]

        peer_uuid = self.get_peer_uuid(peer_spec)
        self.run_ceph_cmd("fs", "snapshot", "mirror", "peer_remove", fs_name, peer_uuid)
        time.sleep(10)
        # verify via asok
        res = self.mirror_daemon_command(f'mirror status for fs: {fs_name}',
                                         'fs', 'mirror', 'status', f'{fs_name}@{fs_id}')
        self.assertTrue(res['peers'] == {} and res['snap_dirs']['dir_count'] == 0)

        res = self.mirror_daemon_command(f'counter dump for fs: {fs_name}', 'counter', 'dump')
        vafter = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_FS][0]

        self.assertLess(vafter["counters"]["mirroring_peers"], vbefore["counters"]["mirroring_peers"])

    def bootstrap_peer(self, fs_name, client_name, site_name):
        outj = json.loads(self.get_ceph_cmd_stdout(
            "fs", "snapshot", "mirror", "peer_bootstrap", "create", fs_name,
            client_name, site_name))
        return outj['token']

    def import_peer(self, fs_name, token):
        self.run_ceph_cmd("fs", "snapshot", "mirror", "peer_bootstrap",
                          "import", fs_name, token)

    def add_directory(self, fs_name, fs_id, dir_name, check_perf_counter=True):
        if check_perf_counter:
            res = self.mirror_daemon_command(f'counter dump for fs: {fs_name}', 'counter', 'dump')
            vbefore = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_FS][0]

        # get initial dir count
        res = self.mirror_daemon_command(f'mirror status for fs: {fs_name}',
                                         'fs', 'mirror', 'status', f'{fs_name}@{fs_id}')
        dir_count = res['snap_dirs']['dir_count']
        log.debug(f'initial dir_count={dir_count}')

        self.run_ceph_cmd("fs", "snapshot", "mirror", "add", fs_name, dir_name)

        time.sleep(10)
        # verify via asok
        res = self.mirror_daemon_command(f'mirror status for fs: {fs_name}',
                                         'fs', 'mirror', 'status', f'{fs_name}@{fs_id}')
        new_dir_count = res['snap_dirs']['dir_count']
        log.debug(f'new dir_count={new_dir_count}')
        self.assertTrue(new_dir_count > dir_count)

        if check_perf_counter:
            res = self.mirror_daemon_command(f'counter dump for fs: {fs_name}', 'counter', 'dump')
            vafter = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_FS][0]
            self.assertGreater(vafter["counters"]["directory_count"], vbefore["counters"]["directory_count"])

    def remove_directory(self, fs_name, fs_id, dir_name):
        res = self.mirror_daemon_command(f'counter dump for fs: {fs_name}', 'counter', 'dump')
        vbefore = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_FS][0]
        # get initial dir count
        res = self.mirror_daemon_command(f'mirror status for fs: {fs_name}',
                                         'fs', 'mirror', 'status', f'{fs_name}@{fs_id}')
        dir_count = res['snap_dirs']['dir_count']
        log.debug(f'initial dir_count={dir_count}')

        self.run_ceph_cmd("fs", "snapshot", "mirror", "remove", fs_name, dir_name)

        time.sleep(10)
        # verify via asok
        res = self.mirror_daemon_command(f'mirror status for fs: {fs_name}',
                                         'fs', 'mirror', 'status', f'{fs_name}@{fs_id}')
        new_dir_count = res['snap_dirs']['dir_count']
        log.debug(f'new dir_count={new_dir_count}')
        self.assertTrue(new_dir_count < dir_count)

        res = self.mirror_daemon_command(f'counter dump for fs: {fs_name}', 'counter', 'dump')
        vafter = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_FS][0]

        self.assertLess(vafter["counters"]["directory_count"], vbefore["counters"]["directory_count"])

    def check_peer_status(self, fs_name, fs_id, peer_spec, dir_name, expected_snap_name,
                          expected_snap_count):
        peer_uuid = self.get_peer_uuid(peer_spec)
        res = self.mirror_daemon_command(f'peer status for fs: {fs_name}',
                                         'fs', 'mirror', 'peer', 'status',
                                         f'{fs_name}@{fs_id}', peer_uuid)
        self.assertTrue(dir_name in res)
        self.assertTrue(res[dir_name]['last_synced_snap']['name'] == expected_snap_name)
        self.assertTrue(res[dir_name]['snaps_synced'] == expected_snap_count)

    def check_peer_status_idle(self, fs_name, fs_id, peer_spec, dir_name, expected_snap_name,
                               expected_snap_count):
        peer_uuid = self.get_peer_uuid(peer_spec)
        res = self.mirror_daemon_command(f'peer status for fs: {fs_name}',
                                         'fs', 'mirror', 'peer', 'status',
                                         f'{fs_name}@{fs_id}', peer_uuid)
        self.assertTrue(dir_name in res)
        self.assertTrue('idle' == res[dir_name]['state'])
        self.assertTrue(expected_snap_name == res[dir_name]['last_synced_snap']['name'])
        self.assertTrue(expected_snap_count == res[dir_name]['snaps_synced'])

    def check_peer_status_deleted_snap(self, fs_name, fs_id, peer_spec, dir_name,
                                      expected_delete_count):
        peer_uuid = self.get_peer_uuid(peer_spec)
        res = self.mirror_daemon_command(f'peer status for fs: {fs_name}',
                                         'fs', 'mirror', 'peer', 'status',
                                         f'{fs_name}@{fs_id}', peer_uuid)
        self.assertTrue(dir_name in res)
        self.assertTrue(res[dir_name]['snaps_deleted'] == expected_delete_count)

    def check_peer_status_renamed_snap(self, fs_name, fs_id, peer_spec, dir_name,
                                       expected_rename_count):
        peer_uuid = self.get_peer_uuid(peer_spec)
        res = self.mirror_daemon_command(f'peer status for fs: {fs_name}',
                                         'fs', 'mirror', 'peer', 'status',
                                         f'{fs_name}@{fs_id}', peer_uuid)
        self.assertTrue(dir_name in res)
        self.assertTrue(res[dir_name]['snaps_renamed'] == expected_rename_count)

    def check_peer_snap_in_progress(self, fs_name, fs_id,
                                    peer_spec, dir_name, snap_name):
        peer_uuid = self.get_peer_uuid(peer_spec)
        res = self.mirror_daemon_command(f'peer status for fs: {fs_name}',
                                         'fs', 'mirror', 'peer', 'status',
                                         f'{fs_name}@{fs_id}', peer_uuid)
        self.assertTrue('syncing' == res[dir_name]['state'])
        self.assertTrue(res[dir_name]['current_syncing_snap']['name'] == snap_name)

    def verify_snapshot(self, dir_name, snap_name):
        snap_list = self.mount_b.ls(path=f'{dir_name}/.snap')
        self.assertTrue(snap_name in snap_list)

        source_res = self.mount_a.dir_checksum(path=f'{dir_name}/.snap/{snap_name}',
                                               follow_symlinks=True)
        log.debug(f'source snapshot checksum {snap_name} {source_res}')

        dest_res = self.mount_b.dir_checksum(path=f'{dir_name}/.snap/{snap_name}',
                                             follow_symlinks=True)
        log.debug(f'destination snapshot checksum {snap_name} {dest_res}')
        self.assertTrue(source_res == dest_res)

    def verify_failed_directory(self, fs_name, fs_id, peer_spec, dir_name):
        peer_uuid = self.get_peer_uuid(peer_spec)
        res = self.mirror_daemon_command(f'peer status for fs: {fs_name}',
                                         'fs', 'mirror', 'peer', 'status',
                                         f'{fs_name}@{fs_id}', peer_uuid)
        self.assertTrue('failed' == res[dir_name]['state'])

    def get_peer_uuid(self, peer_spec):
        status = self.fs.status()
        fs_map = status.get_fsmap_byname(self.primary_fs_name)
        peers = fs_map['mirror_info']['peers']
        for peer_uuid, mirror_info in peers.items():
            client_name = mirror_info['remote']['client_name']
            cluster_name = mirror_info['remote']['cluster_name']
            remote_peer_spec = f'{client_name}@{cluster_name}'
            if peer_spec == remote_peer_spec:
                return peer_uuid
        return None

    def get_daemon_admin_socket(self):
        """overloaded by teuthology override (fs/mirror/clients/mirror.yaml)"""
        return "/var/run/ceph/cephfs-mirror.asok"

    def get_mirror_daemon_pid(self):
        """pid file overloaded in fs/mirror/clients/mirror.yaml"""
        return self.mount_a.run_shell(['cat', '/var/run/ceph/cephfs-mirror.pid']).stdout.getvalue().strip()

    def get_mirror_rados_addr(self, fs_name, fs_id):
        """return the rados addr used by cephfs-mirror instance"""
        res = self.mirror_daemon_command(f'mirror status for fs: {fs_name}',
                                         'fs', 'mirror', 'status', f'{fs_name}@{fs_id}')
        if 'rados_inst' in res:
            return res['rados_inst']

    def mirror_daemon_command(self, cmd_label, *args):
        asok_path = self.get_daemon_admin_socket()
        try:
            # use mount_a's remote to execute command
            p = self.mount_a.client_remote.run(args=
                     ['ceph', '--admin-daemon', asok_path] + list(args),
                     stdout=StringIO(), stderr=StringIO(), timeout=30,
                     check_status=True, label=cmd_label)
            p.wait()
        except CommandFailedError as ce:
            log.warn(f'mirror daemon command with label "{cmd_label}" failed: {ce}')
            raise
        res = p.stdout.getvalue().strip()
        log.debug(f'command returned={res}')
        return json.loads(res)

    def get_mirror_daemon_status(self):
        daemon_status = json.loads(self.get_ceph_cmd_stdout("fs", "snapshot", "mirror", "daemon", "status"))
        log.debug(f'daemon_status: {daemon_status}')
        # running a single mirror daemon is supported
        status = daemon_status[0]
        log.debug(f'status: {status}')
        return status

    def test_basic_mirror_commands(self):
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_mirror_peer_commands(self):
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)

        # add peer
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)
        # remove peer
        self.peer_remove(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph")

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_mirror_disable_with_peer(self):
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)

        # add peer
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_matching_peer(self):
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)

        try:
            self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", check_perf_counter=False)
        except CommandFailedError as ce:
            if ce.exitstatus != errno.EINVAL:
                raise RuntimeError('invalid errno when adding a matching remote peer')
        else:
            raise RuntimeError('adding a peer matching local spec should fail')

        # verify via asok -- nothing should get added
        res = self.mirror_daemon_command(f'mirror status for fs: {self.primary_fs_name}',
                                         'fs', 'mirror', 'status', f'{self.primary_fs_name}@{self.primary_fs_id}')
        self.assertTrue(res['peers'] == {})

        # and explicitly specifying the spec (via filesystem name) should fail too
        try:
            self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.primary_fs_name, check_perf_counter=False)
        except CommandFailedError as ce:
            if ce.exitstatus != errno.EINVAL:
                raise RuntimeError('invalid errno when adding a matching remote peer')
        else:
            raise RuntimeError('adding a peer matching local spec should fail')

        # verify via asok -- nothing should get added
        res = self.mirror_daemon_command(f'mirror status for fs: {self.primary_fs_name}',
                                         'fs', 'mirror', 'status', f'{self.primary_fs_name}@{self.primary_fs_id}')
        self.assertTrue(res['peers'] == {})

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_mirror_peer_add_existing(self):
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)

        # add peer
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)

        # adding the same peer should be idempotent
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name, check_perf_counter=False)

        # remove peer
        self.peer_remove(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph")

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_peer_commands_with_mirroring_disabled(self):
        # try adding peer when mirroring is not enabled
        try:
            self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name, check_perf_counter=False)
        except CommandFailedError as ce:
            if ce.exitstatus != errno.EINVAL:
                raise RuntimeError(-errno.EINVAL, 'incorrect error code when adding a peer')
        else:
            raise RuntimeError(-errno.EINVAL, 'expected peer_add to fail')

        # try removing peer
        try:
            self.run_ceph_cmd("fs", "snapshot", "mirror", "peer_remove", self.primary_fs_name, 'dummy-uuid')
        except CommandFailedError as ce:
            if ce.exitstatus != errno.EINVAL:
                raise RuntimeError(-errno.EINVAL, 'incorrect error code when removing a peer')
        else:
            raise RuntimeError(-errno.EINVAL, 'expected peer_remove to fail')

    def test_add_directory_with_mirroring_disabled(self):
        # try adding a directory when mirroring is not enabled
        try:
            self.add_directory(self.primary_fs_name, self.primary_fs_id, "/d1", check_perf_counter=False)
        except CommandFailedError as ce:
            if ce.exitstatus != errno.EINVAL:
                raise RuntimeError(-errno.EINVAL, 'incorrect error code when adding a directory')
        else:
            raise RuntimeError(-errno.EINVAL, 'expected directory add to fail')

    def test_directory_commands(self):
        self.mount_a.run_shell(["mkdir", "d1"])
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d1')
        try:
            self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d1', check_perf_counter=False)
        except CommandFailedError as ce:
            if ce.exitstatus != errno.EEXIST:
                raise RuntimeError(-errno.EINVAL, 'incorrect error code when re-adding a directory')
        else:
            raise RuntimeError(-errno.EINVAL, 'expected directory add to fail')
        self.remove_directory(self.primary_fs_name, self.primary_fs_id, '/d1')
        try:
            self.remove_directory(self.primary_fs_name, self.primary_fs_id, '/d1')
        except CommandFailedError as ce:
            if ce.exitstatus not in (errno.ENOENT, errno.EINVAL):
                raise RuntimeError(-errno.EINVAL, 'incorrect error code when re-deleting a directory')
        else:
            raise RuntimeError(-errno.EINVAL, 'expected directory removal to fail')
        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.mount_a.run_shell(["rmdir", "d1"])

    def test_directory_command_ls(self):
        dir1 = 'dls1'
        dir2 = 'dls2'
        self.mount_a.run_shell(["mkdir", dir1])
        self.mount_a.run_shell(["mkdir", dir2])
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        try:
            self.add_directory(self.primary_fs_name, self.primary_fs_id, f'/{dir1}')
            self.add_directory(self.primary_fs_name, self.primary_fs_id, f'/{dir2}')
            time.sleep(10)
            dirs_list = json.loads(self.get_ceph_cmd_stdout("fs", "snapshot", "mirror", "ls", self.primary_fs_name))
            # verify via asok
            res = self.mirror_daemon_command(f'mirror status for fs: {self.primary_fs_name}',
                                             'fs', 'mirror', 'status', f'{self.primary_fs_name}@{self.primary_fs_id}')
            dir_count = res['snap_dirs']['dir_count']
            self.assertTrue(len(dirs_list) == dir_count and f'/{dir1}' in dirs_list and f'/{dir2}' in dirs_list)
        except CommandFailedError:
            raise RuntimeError('Error listing directories')
        except AssertionError:
            raise RuntimeError('Wrong number of directories listed')
        finally:
            self.remove_directory(self.primary_fs_name, self.primary_fs_id, f'/{dir1}')
            self.remove_directory(self.primary_fs_name, self.primary_fs_id, f'/{dir2}')

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.mount_a.run_shell(["rmdir", dir1])
        self.mount_a.run_shell(["rmdir",  dir2])

    def test_add_relative_directory_path(self):
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        try:
            self.add_directory(self.primary_fs_name, self.primary_fs_id, './d1', check_perf_counter=False)
        except CommandFailedError as ce:
            if ce.exitstatus != errno.EINVAL:
                raise RuntimeError(-errno.EINVAL, 'incorrect error code when adding a relative path dir')
        else:
            raise RuntimeError(-errno.EINVAL, 'expected directory add to fail')
        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_add_directory_path_normalization(self):
        self.mount_a.run_shell(["mkdir", "-p", "d1/d2/d3"])
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d1/d2/d3')
        def check_add_command_failure(dir_path):
            try:
                self.add_directory(self.primary_fs_name, self.primary_fs_id, dir_path, check_perf_counter=False)
            except CommandFailedError as ce:
                if ce.exitstatus != errno.EEXIST:
                    raise RuntimeError(-errno.EINVAL, 'incorrect error code when re-adding a directory')
            else:
                raise RuntimeError(-errno.EINVAL, 'expected directory add to fail')

        # everything points for /d1/d2/d3
        check_add_command_failure('/d1/d2/././././././d3')
        check_add_command_failure('/d1/d2/././././././d3//////')
        check_add_command_failure('/d1/d2/../d2/././././d3')
        check_add_command_failure('/././././d1/./././d2/./././d3//////')
        check_add_command_failure('/./d1/./d2/./d3/../../../d1/d2/d3')

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.mount_a.run_shell(["rm", "-rf", "d1"])

    def test_add_ancestor_and_child_directory(self):
        self.mount_a.run_shell(["mkdir", "-p", "d1/d2/d3"])
        self.mount_a.run_shell(["mkdir", "-p", "d1/d4"])
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d1/d2/')
        def check_add_command_failure(dir_path):
            try:
                self.add_directory(self.primary_fs_name, self.primary_fs_id, dir_path, check_perf_counter=False)
            except CommandFailedError as ce:
                if ce.exitstatus != errno.EINVAL:
                    raise RuntimeError(-errno.EINVAL, 'incorrect error code when adding a directory')
            else:
                raise RuntimeError(-errno.EINVAL, 'expected directory add to fail')

        # cannot add ancestors or a subtree for an existing directory
        check_add_command_failure('/')
        check_add_command_failure('/d1')
        check_add_command_failure('/d1/d2/d3')

        # obviously, one can add a non-ancestor or non-subtree
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d1/d4/')

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.mount_a.run_shell(["rm", "-rf", "d1"])

    def test_cephfs_mirror_blocklist(self):
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)

        # add peer
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)

        res = self.mirror_daemon_command(f'mirror status for fs: {self.primary_fs_name}',
                                         'fs', 'mirror', 'status', f'{self.primary_fs_name}@{self.primary_fs_id}')
        peers_1 = set(res['peers'])

        # fetch rados address for blacklist check
        rados_inst = self.get_mirror_rados_addr(self.primary_fs_name, self.primary_fs_id)
        self.assertTrue(rados_inst)

        # simulate non-responding mirror daemon by sending SIGSTOP
        pid = self.get_mirror_daemon_pid()
        log.debug(f'SIGSTOP to cephfs-mirror pid {pid}')
        self.mount_a.run_shell(['kill', '-SIGSTOP', pid])

        # wait for blocklist timeout -- the manager module would blocklist
        # the mirror daemon
        time.sleep(40)

        # wake up the mirror daemon -- at this point, the daemon should know
        # that it has been blocklisted
        log.debug('SIGCONT to cephfs-mirror')
        self.mount_a.run_shell(['kill', '-SIGCONT', pid])

        # check if the rados addr is blocklisted
        self.assertTrue(self.mds_cluster.is_addr_blocklisted(rados_inst))

        # wait for restart, which is after 30 seconds timeout (cephfs_mirror_restart_mirror_on_blocklist_interval)
        time.sleep(60)

        # get the new rados_inst
        rados_inst_new = ""
        with safe_while(sleep=2, tries=20, action='wait for mirror status rados_inst') as proceed:
            while proceed():
                rados_inst_new = self.get_mirror_rados_addr(self.primary_fs_name, self.primary_fs_id)
                if rados_inst_new:
                    break

        # and we should get a new rados instance
        self.assertTrue(rados_inst != rados_inst_new)

        # along with peers that were added
        res = self.mirror_daemon_command(f'mirror status for fs: {self.primary_fs_name}',
                                         'fs', 'mirror', 'status', f'{self.primary_fs_name}@{self.primary_fs_id}')
        peers_2 = set(res['peers'])
        self.assertTrue(peers_1, peers_2)

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_cephfs_mirror_stats(self):
        log.debug('reconfigure client auth caps')
        self.get_ceph_cmd_result(
            'auth', 'caps', "client.{0}".format(self.mount_b.client_id),
                'mds', 'allow rw',
                'mon', 'allow r',
                'osd', 'allow rw pool={0}, allow rw pool={1}'.format(
                    self.backup_fs.get_data_pool_name(),
                    self.backup_fs.get_data_pool_name()))

        log.debug(f'mounting filesystem {self.secondary_fs_name}')
        self.mount_b.umount_wait()
        self.mount_b.mount_wait(cephfs_name=self.secondary_fs_name)

        # create a bunch of files in a directory to snap
        self.mount_a.run_shell(["mkdir", "d0"])
        for i in range(10):
            self.mount_a.write_n_mb(os.path.join('d0', f'file.{i}'), 100)

        time.sleep(60)
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d0')
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)

        # dump perf counters
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        first = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]

        # take a snapshot
        self.mount_a.run_shell(["mkdir", "d0/.snap/snap0"])

        time.sleep(120)
        self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                               "client.mirror_remote@ceph", '/d0', 'snap0', 1)
        self.verify_snapshot('d0', 'snap0')

        # check perf counters
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        second = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(second["counters"]["snaps_synced"], first["counters"]["snaps_synced"])
        self.assertGreater(second["counters"]["last_synced_start"], first["counters"]["last_synced_start"])
        self.assertGreaterEqual(second["counters"]["last_synced_end"], second["counters"]["last_synced_start"])
        self.assertGreater(second["counters"]["last_synced_duration"], 0)
        self.assertEquals(second["counters"]["last_synced_bytes"], 1048576000) # last_synced_bytes = 10 files of 100MB size each

        # some more IO
        for i in range(15):
            self.mount_a.write_n_mb(os.path.join('d0', f'more_file.{i}'), 100)

        time.sleep(60)

        # take another snapshot
        self.mount_a.run_shell(["mkdir", "d0/.snap/snap1"])

        time.sleep(240)
        self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                               "client.mirror_remote@ceph", '/d0', 'snap1', 2)
        self.verify_snapshot('d0', 'snap1')

        # check perf counters
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        third = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(third["counters"]["snaps_synced"], second["counters"]["snaps_synced"])
        self.assertGreater(third["counters"]["last_synced_start"], second["counters"]["last_synced_end"])
        self.assertGreaterEqual(third["counters"]["last_synced_end"], third["counters"]["last_synced_start"])
        self.assertGreater(third["counters"]["last_synced_duration"], 0)
        self.assertEquals(third["counters"]["last_synced_bytes"], 1572864000) # last_synced_bytes = 15 files of 100MB size each

        # delete a snapshot
        self.mount_a.run_shell(["rmdir", "d0/.snap/snap0"])

        time.sleep(10)
        snap_list = self.mount_b.ls(path='d0/.snap')
        self.assertTrue('snap0' not in snap_list)
        self.check_peer_status_deleted_snap(self.primary_fs_name, self.primary_fs_id,
                                            "client.mirror_remote@ceph", '/d0', 1)
        # check snaps_deleted
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        fourth = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(fourth["counters"]["snaps_deleted"], third["counters"]["snaps_deleted"])

        # rename a snapshot
        self.mount_a.run_shell(["mv", "d0/.snap/snap1", "d0/.snap/snap2"])

        time.sleep(10)
        snap_list = self.mount_b.ls(path='d0/.snap')
        self.assertTrue('snap1' not in snap_list)
        self.assertTrue('snap2' in snap_list)
        self.check_peer_status_renamed_snap(self.primary_fs_name, self.primary_fs_id,
                                            "client.mirror_remote@ceph", '/d0', 1)
        # check snaps_renamed
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        fifth = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(fifth["counters"]["snaps_renamed"], fourth["counters"]["snaps_renamed"])

        self.remove_directory(self.primary_fs_name, self.primary_fs_id, '/d0')
        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_cephfs_mirror_cancel_sync(self):
        log.debug('reconfigure client auth caps')
        self.get_ceph_cmd_result(
            'auth', 'caps', "client.{0}".format(self.mount_b.client_id),
                'mds', 'allow rw',
                'mon', 'allow r',
                'osd', 'allow rw pool={0}, allow rw pool={1}'.format(
                    self.backup_fs.get_data_pool_name(),
                    self.backup_fs.get_data_pool_name()))

        log.debug(f'mounting filesystem {self.secondary_fs_name}')
        self.mount_b.umount_wait()
        self.mount_b.mount_wait(cephfs_name=self.secondary_fs_name)

        # create a bunch of files in a directory to snap
        self.mount_a.run_shell(["mkdir", "d0"])
        for i in range(100):
            filename = f'file.{i}'
            self.mount_a.write_n_mb(os.path.join('d0', filename), 1024)

        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d0')
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)

        # take a snapshot
        self.mount_a.run_shell(["mkdir", "d0/.snap/snap0"])

        time.sleep(10)
        self.check_peer_snap_in_progress(self.primary_fs_name, self.primary_fs_id,
                                         "client.mirror_remote@ceph", '/d0', 'snap0')

        self.remove_directory(self.primary_fs_name, self.primary_fs_id, '/d0')

        snap_list = self.mount_b.ls(path='d0/.snap')
        self.assertTrue('snap0' not in snap_list)

        # check sync_failures
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vmirror_peers = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(vmirror_peers["counters"]["sync_failures"], 0)

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_cephfs_mirror_restart_sync_on_blocklist(self):
        log.debug('reconfigure client auth caps')
        self.get_ceph_cmd_result(
            'auth', 'caps', "client.{0}".format(self.mount_b.client_id),
                'mds', 'allow rw',
                'mon', 'allow r',
                'osd', 'allow rw pool={0}, allow rw pool={1}'.format(
                    self.backup_fs.get_data_pool_name(),
                    self.backup_fs.get_data_pool_name()))

        log.debug(f'mounting filesystem {self.secondary_fs_name}')
        self.mount_b.umount_wait()
        self.mount_b.mount_wait(cephfs_name=self.secondary_fs_name)

        # create a bunch of files in a directory to snap
        self.mount_a.run_shell(["mkdir", "d0"])
        for i in range(8):
            filename = f'file.{i}'
            self.mount_a.write_n_mb(os.path.join('d0', filename), 1024)

        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d0')
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)

        # fetch rados address for blacklist check
        rados_inst = self.get_mirror_rados_addr(self.primary_fs_name, self.primary_fs_id)

        # dump perf counters
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vbefore = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]

        # take a snapshot
        self.mount_a.run_shell(["mkdir", "d0/.snap/snap0"])

        time.sleep(10)
        self.check_peer_snap_in_progress(self.primary_fs_name, self.primary_fs_id,
                                         "client.mirror_remote@ceph", '/d0', 'snap0')

        # simulate non-responding mirror daemon by sending SIGSTOP
        pid = self.get_mirror_daemon_pid()
        log.debug(f'SIGSTOP to cephfs-mirror pid {pid}')
        self.mount_a.run_shell(['kill', '-SIGSTOP', pid])

        # wait for blocklist timeout -- the manager module would blocklist
        # the mirror daemon
        time.sleep(40)

        # wake up the mirror daemon -- at this point, the daemon should know
        # that it has been blocklisted
        log.debug('SIGCONT to cephfs-mirror')
        self.mount_a.run_shell(['kill', '-SIGCONT', pid])

        # check if the rados addr is blocklisted
        self.assertTrue(self.mds_cluster.is_addr_blocklisted(rados_inst))

        time.sleep(500)
        self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                               "client.mirror_remote@ceph", '/d0', 'snap0', expected_snap_count=1)
        self.verify_snapshot('d0', 'snap0')
        # check snaps_synced
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vafter = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(vafter["counters"]["snaps_synced"], vbefore["counters"]["snaps_synced"])

        self.remove_directory(self.primary_fs_name, self.primary_fs_id, '/d0')
        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_cephfs_mirror_failed_sync_with_correction(self):
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)

        # dump perf counters
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vfirst = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]

        # add a non-existent directory for synchronization
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d0')

        # wait for mirror daemon to mark it the directory as failed
        time.sleep(120)
        self.verify_failed_directory(self.primary_fs_name, self.primary_fs_id,
                                     "client.mirror_remote@ceph", '/d0')

        # create the directory
        self.mount_a.run_shell(["mkdir", "d0"])
        self.mount_a.run_shell(["mkdir", "d0/.snap/snap0"])

        # wait for correction
        time.sleep(120)
        self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                               "client.mirror_remote@ceph", '/d0', 'snap0', 1)
        # check snaps_synced
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vsecond = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(vsecond["counters"]["snaps_synced"], vfirst["counters"]["snaps_synced"])
        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_cephfs_mirror_service_daemon_status(self):
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)

        time.sleep(30)
        status = self.get_mirror_daemon_status()

        # assumption for this test: mirroring enabled for a single filesystem w/ single
        # peer

        # we have not added any directories
        peer = status['filesystems'][0]['peers'][0]
        self.assertEquals(status['filesystems'][0]['directory_count'], 0)
        self.assertEquals(peer['stats']['failure_count'], 0)
        self.assertEquals(peer['stats']['recovery_count'], 0)

        # add a non-existent directory for synchronization -- check if its reported
        # in daemon stats
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d0')

        time.sleep(120)
        status = self.get_mirror_daemon_status()
        # we added one
        peer = status['filesystems'][0]['peers'][0]
        self.assertEquals(status['filesystems'][0]['directory_count'], 1)
        # failure count should be reflected
        self.assertEquals(peer['stats']['failure_count'], 1)
        self.assertEquals(peer['stats']['recovery_count'], 0)

        # create the directory, mirror daemon would recover
        self.mount_a.run_shell(["mkdir", "d0"])

        time.sleep(120)
        status = self.get_mirror_daemon_status()
        peer = status['filesystems'][0]['peers'][0]
        self.assertEquals(status['filesystems'][0]['directory_count'], 1)
        # failure and recovery count should be reflected
        self.assertEquals(peer['stats']['failure_count'], 1)
        self.assertEquals(peer['stats']['recovery_count'], 1)

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_mirroring_init_failure(self):
        """Test mirror daemon init failure"""

        # disable mgr mirroring plugin as it would try to load dir map on
        # on mirroring enabled for a filesystem (an throw up erorrs in
        # the logs)
        self.disable_mirroring_module()

        # enable mirroring through mon interface -- this should result in the mirror daemon
        # failing to enable mirroring due to absence of `cephfs_mirror` index object.
        self.run_ceph_cmd("fs", "mirror", "enable", self.primary_fs_name)

        with safe_while(sleep=5, tries=10, action='wait for failed state') as proceed:
            while proceed():
                try:
                    # verify via asok
                    res = self.mirror_daemon_command(f'mirror status for fs: {self.primary_fs_name}',
                                                     'fs', 'mirror', 'status', f'{self.primary_fs_name}@{self.primary_fs_id}')
                    if not 'state' in res:
                        return
                    self.assertTrue(res['state'] == "failed")
                    return True
                except:
                    pass

        self.run_ceph_cmd("fs", "mirror", "disable", self.primary_fs_name)
        time.sleep(10)
        # verify via asok
        try:
            self.mirror_daemon_command(f'mirror status for fs: {self.primary_fs_name}',
                                       'fs', 'mirror', 'status', f'{self.primary_fs_name}@{self.primary_fs_id}')
        except CommandFailedError:
            pass
        else:
            raise RuntimeError('expected admin socket to be unavailable')

    def test_mirroring_init_failure_with_recovery(self):
        """Test if the mirror daemon can recover from a init failure"""

        # disable mgr mirroring plugin as it would try to load dir map on
        # on mirroring enabled for a filesystem (an throw up erorrs in
        # the logs)
        self.disable_mirroring_module()

        # enable mirroring through mon interface -- this should result in the mirror daemon
        # failing to enable mirroring due to absence of `cephfs_mirror` index object.

        self.run_ceph_cmd("fs", "mirror", "enable", self.primary_fs_name)
        # need safe_while since non-failed status pops up as mirroring is restarted
        # internally in mirror daemon.
        with safe_while(sleep=5, tries=20, action='wait for failed state') as proceed:
            while proceed():
                try:
                    # verify via asok
                    res = self.mirror_daemon_command(f'mirror status for fs: {self.primary_fs_name}',
                                                     'fs', 'mirror', 'status', f'{self.primary_fs_name}@{self.primary_fs_id}')
                    if not 'state' in res:
                        return
                    self.assertTrue(res['state'] == "failed")
                    return True
                except:
                    pass

        # create the index object and check daemon recovery
        try:
            p = self.mount_a.client_remote.run(args=['rados', '-p', self.fs.metadata_pool_name, 'create', 'cephfs_mirror'],
                                               stdout=StringIO(), stderr=StringIO(), timeout=30,
                                               check_status=True, label="create index object")
            p.wait()
        except CommandFailedError as ce:
            log.warn(f'mirror daemon command to create mirror index object failed: {ce}')
            raise
        time.sleep(30)
        res = self.mirror_daemon_command(f'mirror status for fs: {self.primary_fs_name}',
                                         'fs', 'mirror', 'status', f'{self.primary_fs_name}@{self.primary_fs_id}')
        self.assertTrue(res['peers'] == {})
        self.assertTrue(res['snap_dirs']['dir_count'] == 0)

        self.run_ceph_cmd("fs", "mirror", "disable", self.primary_fs_name)
        time.sleep(10)
        # verify via asok
        try:
            self.mirror_daemon_command(f'mirror status for fs: {self.primary_fs_name}',
                                       'fs', 'mirror', 'status', f'{self.primary_fs_name}@{self.primary_fs_id}')
        except CommandFailedError:
            pass
        else:
            raise RuntimeError('expected admin socket to be unavailable')

    def test_cephfs_mirror_peer_bootstrap(self):
        """Test importing peer bootstrap token"""
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)

        # create a bootstrap token for the peer
        bootstrap_token = self.bootstrap_peer(self.secondary_fs_name, "client.mirror_peer_bootstrap", "site-remote")

        # import the peer via bootstrap token
        self.import_peer(self.primary_fs_name, bootstrap_token)
        time.sleep(10)
        self.verify_peer_added(self.primary_fs_name, self.primary_fs_id, "client.mirror_peer_bootstrap@site-remote",
                               self.secondary_fs_name)

        # verify via peer_list interface
        peer_uuid = self.get_peer_uuid("client.mirror_peer_bootstrap@site-remote")
        res = json.loads(self.get_ceph_cmd_stdout("fs", "snapshot", "mirror", "peer_list", self.primary_fs_name))
        self.assertTrue(peer_uuid in res)

        # remove peer
        self.peer_remove(self.primary_fs_name, self.primary_fs_id, "client.mirror_peer_bootstrap@site-remote")
        # disable mirroring
        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_cephfs_mirror_symlink_sync(self):
        log.debug('reconfigure client auth caps')
        self.get_ceph_cmd_result(
            'auth', 'caps', "client.{0}".format(self.mount_b.client_id),
                'mds', 'allow rw',
                'mon', 'allow r',
                'osd', 'allow rw pool={0}, allow rw pool={1}'.format(
                    self.backup_fs.get_data_pool_name(),
                    self.backup_fs.get_data_pool_name()))

        log.debug(f'mounting filesystem {self.secondary_fs_name}')
        self.mount_b.umount_wait()
        self.mount_b.mount_wait(cephfs_name=self.secondary_fs_name)

        # create a bunch of files w/ symbolic links in a directory to snap
        self.mount_a.run_shell(["mkdir", "d0"])
        self.mount_a.create_n_files('d0/file', 10, sync=True)
        self.mount_a.run_shell(["ln", "-s", "./file_0", "d0/sym_0"])
        self.mount_a.run_shell(["ln", "-s", "./file_1", "d0/sym_1"])
        self.mount_a.run_shell(["ln", "-s", "./file_2", "d0/sym_2"])

        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d0')
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)

        # dump perf counters
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vbefore = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]

        # take a snapshot
        self.mount_a.run_shell(["mkdir", "d0/.snap/snap0"])

        time.sleep(30)
        self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                               "client.mirror_remote@ceph", '/d0', 'snap0', 1)
        self.verify_snapshot('d0', 'snap0')

        # check snaps_synced
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vafter = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(vafter["counters"]["snaps_synced"], vbefore["counters"]["snaps_synced"])
        self.remove_directory(self.primary_fs_name, self.primary_fs_id, '/d0')
        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_cephfs_mirror_with_parent_snapshot(self):
        """Test snapshot synchronization with parent directory snapshots"""
        self.mount_a.run_shell(["mkdir", "-p", "d0/d1/d2/d3"])

        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d0/d1/d2/d3')
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)

        # dump perf counters
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vfirst = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]

        # take a snapshot
        self.mount_a.run_shell(["mkdir", "d0/d1/d2/d3/.snap/snap0"])

        time.sleep(30)
        self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                               "client.mirror_remote@ceph", '/d0/d1/d2/d3', 'snap0', 1)
        # check snaps_synced
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vsecond = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(vsecond["counters"]["snaps_synced"], vfirst["counters"]["snaps_synced"])

        # create snapshots in parent directories
        self.mount_a.run_shell(["mkdir", "d0/.snap/snap_d0"])
        self.mount_a.run_shell(["mkdir", "d0/d1/.snap/snap_d1"])
        self.mount_a.run_shell(["mkdir", "d0/d1/d2/.snap/snap_d2"])

        # try syncing more snapshots
        self.mount_a.run_shell(["mkdir", "d0/d1/d2/d3/.snap/snap1"])
        time.sleep(30)
        self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                               "client.mirror_remote@ceph", '/d0/d1/d2/d3', 'snap1', 2)
        # check snaps_synced
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vthird = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(vthird["counters"]["snaps_synced"], vsecond["counters"]["snaps_synced"])

        self.mount_a.run_shell(["rmdir", "d0/d1/d2/d3/.snap/snap0"])
        self.mount_a.run_shell(["rmdir", "d0/d1/d2/d3/.snap/snap1"])
        time.sleep(15)
        self.check_peer_status_deleted_snap(self.primary_fs_name, self.primary_fs_id,
                                            "client.mirror_remote@ceph", '/d0/d1/d2/d3', 2)
        # check snaps_deleted
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vfourth = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(vfourth["counters"]["snaps_deleted"], vthird["counters"]["snaps_deleted"])

        self.remove_directory(self.primary_fs_name, self.primary_fs_id, '/d0/d1/d2/d3')
        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_cephfs_mirror_remove_on_stall(self):
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)

        # fetch rados address for blacklist check
        rados_inst = self.get_mirror_rados_addr(self.primary_fs_name, self.primary_fs_id)

        # simulate non-responding mirror daemon by sending SIGSTOP
        pid = self.get_mirror_daemon_pid()
        log.debug(f'SIGSTOP to cephfs-mirror pid {pid}')
        self.mount_a.run_shell(['kill', '-SIGSTOP', pid])

        # wait for blocklist timeout -- the manager module would blocklist
        # the mirror daemon
        time.sleep(40)

        # make sure the rados addr is blocklisted
        self.assertTrue(self.mds_cluster.is_addr_blocklisted(rados_inst))

        # now we are sure that there are no "active" mirror daemons -- add a directory path.
        dir_path_p = "/d0/d1"
        dir_path = "/d0/d1/d2"

        self.run_ceph_cmd("fs", "snapshot", "mirror", "add", self.primary_fs_name, dir_path)

        time.sleep(10)
        # this uses an undocumented interface to get dirpath map state
        res_json = self.get_ceph_cmd_stdout("fs", "snapshot", "mirror", "dirmap", self.primary_fs_name, dir_path)
        res = json.loads(res_json)
        # there are no mirror daemons
        self.assertTrue(res['state'], 'stalled')

        self.run_ceph_cmd("fs", "snapshot", "mirror", "remove", self.primary_fs_name, dir_path)

        time.sleep(10)
        try:
            self.run_ceph_cmd("fs", "snapshot", "mirror", "dirmap", self.primary_fs_name, dir_path)
        except CommandFailedError as ce:
            if ce.exitstatus != errno.ENOENT:
                raise RuntimeError('invalid errno when checking dirmap status for non-existent directory')
        else:
            raise RuntimeError('incorrect errno when checking dirmap state for non-existent directory')

        # adding a parent directory should be allowed
        self.run_ceph_cmd("fs", "snapshot", "mirror", "add", self.primary_fs_name, dir_path_p)

        time.sleep(10)
        # however, this directory path should get stalled too
        res_json = self.get_ceph_cmd_stdout("fs", "snapshot", "mirror", "dirmap", self.primary_fs_name, dir_path_p)
        res = json.loads(res_json)
        # there are no mirror daemons
        self.assertTrue(res['state'], 'stalled')

        # wake up the mirror daemon -- at this point, the daemon should know
        # that it has been blocklisted
        log.debug('SIGCONT to cephfs-mirror')
        self.mount_a.run_shell(['kill', '-SIGCONT', pid])

        # wait for restart mirror on blocklist
        time.sleep(60)
        res_json = self.get_ceph_cmd_stdout("fs", "snapshot", "mirror", "dirmap", self.primary_fs_name, dir_path_p)
        res = json.loads(res_json)
        # there are no mirror daemons
        self.assertTrue(res['state'], 'mapped')

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_cephfs_mirror_incremental_sync(self):
        """ Test incremental snapshot synchronization (based on mtime differences)."""
        log.debug('reconfigure client auth caps')
        self.get_ceph_cmd_result(
            'auth', 'caps', "client.{0}".format(self.mount_b.client_id),
            'mds', 'allow rw',
            'mon', 'allow r',
            'osd', 'allow rw pool={0}, allow rw pool={1}'.format(
                self.backup_fs.get_data_pool_name(),
                self.backup_fs.get_data_pool_name()))
        log.debug(f'mounting filesystem {self.secondary_fs_name}')
        self.mount_b.umount_wait()
        self.mount_b.mount_wait(cephfs_name=self.secondary_fs_name)

        repo = 'ceph-qa-suite'
        repo_dir = 'ceph_repo'
        repo_path = f'{repo_dir}/{repo}'

        def clone_repo():
            self.mount_a.run_shell([
                'git', 'clone', '--branch', 'giant',
                f'http://github.com/ceph/{repo}', repo_path])

        def exec_git_cmd(cmd_list):
            self.mount_a.run_shell(['git', '--git-dir', f'{self.mount_a.mountpoint}/{repo_path}/.git', *cmd_list])

        self.mount_a.run_shell(["mkdir", repo_dir])
        clone_repo()

        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)

        self.add_directory(self.primary_fs_name, self.primary_fs_id, f'/{repo_path}')
        # dump perf counters
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vfirst = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.mount_a.run_shell(['mkdir', f'{repo_path}/.snap/snap_a'])

        # full copy, takes time
        time.sleep(500)
        self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                               "client.mirror_remote@ceph", f'/{repo_path}', 'snap_a', 1)
        self.verify_snapshot(repo_path, 'snap_a')
        # check snaps_synced
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vsecond = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(vsecond["counters"]["snaps_synced"], vfirst["counters"]["snaps_synced"])

        # create some diff
        num = random.randint(5, 20)
        log.debug(f'resetting to HEAD~{num}')
        exec_git_cmd(["reset", "--hard", f'HEAD~{num}'])

        self.mount_a.run_shell(['mkdir', f'{repo_path}/.snap/snap_b'])
        # incremental copy, should be fast
        time.sleep(180)
        self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                               "client.mirror_remote@ceph", f'/{repo_path}', 'snap_b', 2)
        self.verify_snapshot(repo_path, 'snap_b')
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vthird = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(vthird["counters"]["snaps_synced"], vsecond["counters"]["snaps_synced"])

        # diff again, this time back to HEAD
        log.debug('resetting to HEAD')
        exec_git_cmd(["pull"])

        self.mount_a.run_shell(['mkdir', f'{repo_path}/.snap/snap_c'])
        # incremental copy, should be fast
        time.sleep(180)
        self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                               "client.mirror_remote@ceph", f'/{repo_path}', 'snap_c', 3)
        self.verify_snapshot(repo_path, 'snap_c')
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vfourth = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(vfourth["counters"]["snaps_synced"], vthird["counters"]["snaps_synced"])

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_cephfs_mirror_incremental_sync_with_type_mixup(self):
        """ Test incremental snapshot synchronization with file type changes.

        The same filename exist as a different type in subsequent snapshot.
        This verifies if the mirror daemon can identify file type mismatch and
        sync snapshots.

              \    snap_0       snap_1      snap_2      snap_3
               \-----------------------------------------------
        file_x |   reg          sym         dir         reg
               |
        file_y |   dir          reg         sym         dir
               |
        file_z |   sym          dir         reg         sym
        """
        log.debug('reconfigure client auth caps')
        self.get_ceph_cmd_result(
            'auth', 'caps', "client.{0}".format(self.mount_b.client_id),
                'mds', 'allow rw',
                'mon', 'allow r',
                'osd', 'allow rw pool={0}, allow rw pool={1}'.format(
                    self.backup_fs.get_data_pool_name(),
                    self.backup_fs.get_data_pool_name()))
        log.debug(f'mounting filesystem {self.secondary_fs_name}')
        self.mount_b.umount_wait()
        self.mount_b.mount_wait(cephfs_name=self.secondary_fs_name)

        typs = deque(['reg', 'dir', 'sym'])
        def cleanup_and_create_with_type(dirname, fnames):
            self.mount_a.run_shell_payload(f"rm -rf {dirname}/*")
            fidx = 0
            for t in typs:
                fname = f'{dirname}/{fnames[fidx]}'
                log.debug(f'file: {fname} type: {t}')
                if t == 'reg':
                    self.mount_a.run_shell(["touch", fname])
                    self.mount_a.write_file(fname, data=fname)
                elif t == 'dir':
                    self.mount_a.run_shell(["mkdir", fname])
                elif t == 'sym':
                    # verify ELOOP in mirror daemon
                    self.mount_a.run_shell(["ln", "-s", "..", fname])
                fidx += 1

        def verify_types(dirname, fnames, snap_name):
            tidx = 0
            for fname in fnames:
                t = self.mount_b.run_shell_payload(f"stat -c %F {dirname}/.snap/{snap_name}/{fname}").stdout.getvalue().strip()
                if typs[tidx] == 'reg':
                    self.assertEquals('regular file', t)
                elif typs[tidx] == 'dir':
                    self.assertEquals('directory', t)
                elif typs[tidx] == 'sym':
                    self.assertEquals('symbolic link', t)
                tidx += 1

        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)

        self.mount_a.run_shell(["mkdir", "d0"])
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d0')

        fnames = ['file_x', 'file_y', 'file_z']
        turns = 0
        while turns != len(typs):
            snapname = f'snap_{turns}'
            cleanup_and_create_with_type('d0', fnames)
            # dump perf counters
            res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
            vbefore = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
            self.mount_a.run_shell(['mkdir', f'd0/.snap/{snapname}'])
            time.sleep(30)
            self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                                   "client.mirror_remote@ceph", '/d0', snapname, turns+1)
            verify_types('d0', fnames, snapname)
            res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
            vafter = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
            self.assertGreater(vafter["counters"]["snaps_synced"], vbefore["counters"]["snaps_synced"])

            # next type
            typs.rotate(1)
            turns += 1

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_cephfs_mirror_sync_with_purged_snapshot(self):
        """Test snapshot synchronization in midst of snapshot deletes.

        Deleted the previous snapshot when the mirror daemon is figuring out
        incremental differences between current and previous snaphot. The
        mirror daemon should identify the purge and switch to using remote
        comparison to sync the snapshot (in the next iteration of course).
        """

        log.debug('reconfigure client auth caps')
        self.get_ceph_cmd_result(
            'auth', 'caps', "client.{0}".format(self.mount_b.client_id),
            'mds', 'allow rw',
            'mon', 'allow r',
            'osd', 'allow rw pool={0}, allow rw pool={1}'.format(
                self.backup_fs.get_data_pool_name(),
                self.backup_fs.get_data_pool_name()))
        log.debug(f'mounting filesystem {self.secondary_fs_name}')
        self.mount_b.umount_wait()
        self.mount_b.mount_wait(cephfs_name=self.secondary_fs_name)

        repo = 'ceph-qa-suite'
        repo_dir = 'ceph_repo'
        repo_path = f'{repo_dir}/{repo}'

        def clone_repo():
            self.mount_a.run_shell([
                'git', 'clone', '--branch', 'giant',
                f'http://github.com/ceph/{repo}', repo_path])

        def exec_git_cmd(cmd_list):
            self.mount_a.run_shell(['git', '--git-dir', f'{self.mount_a.mountpoint}/{repo_path}/.git', *cmd_list])

        self.mount_a.run_shell(["mkdir", repo_dir])
        clone_repo()

        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)

        self.add_directory(self.primary_fs_name, self.primary_fs_id, f'/{repo_path}')
        # dump perf counters
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vfirst = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.mount_a.run_shell(['mkdir', f'{repo_path}/.snap/snap_a'])

        # full copy, takes time
        time.sleep(500)
        self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                               "client.mirror_remote@ceph", f'/{repo_path}', 'snap_a', 1)
        self.verify_snapshot(repo_path, 'snap_a')
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vsecond = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(vsecond["counters"]["snaps_synced"], vfirst["counters"]["snaps_synced"])

        # create some diff
        num = random.randint(60, 100)
        log.debug(f'resetting to HEAD~{num}')
        exec_git_cmd(["reset", "--hard", f'HEAD~{num}'])

        self.mount_a.run_shell(['mkdir', f'{repo_path}/.snap/snap_b'])

        time.sleep(15)
        self.mount_a.run_shell(['rmdir', f'{repo_path}/.snap/snap_a'])

        # incremental copy but based on remote dir_root
        time.sleep(300)
        self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                               "client.mirror_remote@ceph", f'/{repo_path}', 'snap_b', 2)
        self.verify_snapshot(repo_path, 'snap_b')
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vthird = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(vthird["counters"]["snaps_synced"], vsecond["counters"]["snaps_synced"])

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_cephfs_mirror_peer_add_primary(self):
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)

        # try adding the primary file system as a peer to secondary file
        # system
        try:
            self.peer_add(self.secondary_fs_name, self.secondary_fs_id, "client.mirror_remote@ceph", self.primary_fs_name, check_perf_counter=False)
        except CommandFailedError as ce:
            if ce.exitstatus != errno.EINVAL:
                raise RuntimeError('invalid errno when adding a primary file system')
        else:
            raise RuntimeError('adding peer should fail')

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_cephfs_mirror_cancel_mirroring_and_readd(self):
        """
        Test adding a directory path for synchronization post removal of already added directory paths

        ... to ensure that synchronization of the newly added directory path functions
        as expected. Note that we schedule three (3) directories for mirroring to ensure
        that all replayer threads (3 by default) in the mirror daemon are busy.
        """
        log.debug('reconfigure client auth caps')
        self.get_ceph_cmd_result(
            'auth', 'caps', "client.{0}".format(self.mount_b.client_id),
                'mds', 'allow rw',
                'mon', 'allow r',
                'osd', 'allow rw pool={0}, allow rw pool={1}'.format(
                    self.backup_fs.get_data_pool_name(),
                    self.backup_fs.get_data_pool_name()))

        log.debug(f'mounting filesystem {self.secondary_fs_name}')
        self.mount_b.umount_wait()
        self.mount_b.mount_wait(cephfs_name=self.secondary_fs_name)

        # create some large files in 3 directories to snap
        self.mount_a.run_shell(["mkdir", "d0"])
        self.mount_a.run_shell(["mkdir", "d1"])
        self.mount_a.run_shell(["mkdir", "d2"])
        for i in range(4):
            filename = f'file.{i}'
            self.mount_a.write_n_mb(os.path.join('d0', filename), 1024)
            self.mount_a.write_n_mb(os.path.join('d1', filename), 1024)
            self.mount_a.write_n_mb(os.path.join('d2', filename), 1024)

        log.debug('enabling mirroring')
        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        log.debug('adding directory paths')
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d0')
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d1')
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d2')
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)

        # dump perf counters
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vbefore = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        # take snapshots
        log.debug('taking snapshots')
        snap_name = "snap0"
        self.mount_a.run_shell(["mkdir", f"d0/.snap/{snap_name}"])
        self.mount_a.run_shell(["mkdir", f"d1/.snap/{snap_name}"])
        self.mount_a.run_shell(["mkdir", f"d2/.snap/{snap_name}"])

        log.debug('checking snap in progress')
        peer_spec = "client.mirror_remote@ceph"
        peer_uuid = self.get_peer_uuid(peer_spec)
        with safe_while(sleep=3, tries=100, action=f'wait for status: {peer_spec}') as proceed:
            while proceed():
                res = self.mirror_daemon_command(f'peer status for fs: {self.primary_fs_name}',
                                                 'fs', 'mirror', 'peer', 'status',
                                                 f'{self.primary_fs_name}@{self.primary_fs_id}',
                                                 peer_uuid)
                if ('syncing' == res["/d0"]['state'] and 'syncing' == res["/d1"]['state'] and \
                    'syncing' == res["/d2"]['state']):
                    break

        log.debug('removing directory 1')
        self.remove_directory(self.primary_fs_name, self.primary_fs_id, '/d0')
        log.debug('removing directory 2')
        self.remove_directory(self.primary_fs_name, self.primary_fs_id, '/d1')
        log.debug('removing directory 3')
        self.remove_directory(self.primary_fs_name, self.primary_fs_id, '/d2')

        # Wait a while for the sync backoff
        time.sleep(500)

        log.debug('removing snapshots')
        self.mount_a.run_shell(["rmdir", f"d0/.snap/{snap_name}"])
        self.mount_a.run_shell(["rmdir", f"d1/.snap/{snap_name}"])
        self.mount_a.run_shell(["rmdir", f"d2/.snap/{snap_name}"])

        for i in range(4):
            filename = f'file.{i}'
            log.debug(f'deleting {filename}')
            self.mount_a.run_shell(["rm", "-f", os.path.join('d0', filename)])
            self.mount_a.run_shell(["rm", "-f", os.path.join('d1', filename)])
            self.mount_a.run_shell(["rm", "-f", os.path.join('d2', filename)])

        log.debug('creating new files...')
        self.mount_a.create_n_files('d0/file', 50, sync=True)
        self.mount_a.create_n_files('d1/file', 50, sync=True)
        self.mount_a.create_n_files('d2/file', 50, sync=True)

        log.debug('adding directory paths')
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d0')
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d1')
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/d2')

        log.debug('creating new snapshots...')
        self.mount_a.run_shell(["mkdir", f"d0/.snap/{snap_name}"])
        self.mount_a.run_shell(["mkdir", f"d1/.snap/{snap_name}"])
        self.mount_a.run_shell(["mkdir", f"d2/.snap/{snap_name}"])

        # Wait for the threads to finish
        time.sleep(500)

        self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                               "client.mirror_remote@ceph", '/d0', f'{snap_name}', 1)
        self.verify_snapshot('d0', f'{snap_name}')

        self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                               "client.mirror_remote@ceph", '/d1', f'{snap_name}', 1)
        self.verify_snapshot('d1', f'{snap_name}')

        self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                               "client.mirror_remote@ceph", '/d2', f'{snap_name}', 1)
        self.verify_snapshot('d2', f'{snap_name}')
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vafter = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        self.assertGreater(vafter["counters"]["snaps_synced"], vbefore["counters"]["snaps_synced"])

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_local_and_remote_dir_root_mode(self):
        log.debug('reconfigure client auth caps')
        cid = self.mount_b.client_id
        data_pool = self.backup_fs.get_data_pool_name()
        self.get_ceph_cmd_result(
            'auth', 'caps', f"client.{cid}",
            'mds', 'allow rw',
            'mon', 'allow r',
            'osd', f"allow rw pool={data_pool}, allow rw pool={data_pool}")

        log.debug(f'mounting filesystem {self.secondary_fs_name}')
        self.mount_b.umount_wait()
        self.mount_b.mount_wait(cephfs_name=self.secondary_fs_name)

        self.mount_a.run_shell(["mkdir", "l1"])
        self.mount_a.run_shell(["mkdir", "l1/.snap/snap0"])
        self.mount_a.run_shell(["chmod", "go-rwx", "l1"])

        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.add_directory(self.primary_fs_name, self.primary_fs_id, '/l1')
        self.peer_add(self.primary_fs_name, self.primary_fs_id, "client.mirror_remote@ceph", self.secondary_fs_name)

        time.sleep(60)
        self.check_peer_status(self.primary_fs_name, self.primary_fs_id,
                               "client.mirror_remote@ceph", '/l1', 'snap0', 1)
        # dump perf counters
        res = self.mirror_daemon_command(f'counter dump for fs: {self.primary_fs_name}', 'counter', 'dump')
        vmirror_peers = res[TestMirroring.PERF_COUNTER_KEY_NAME_CEPHFS_MIRROR_PEER][0]
        snaps_synced = vmirror_peers["counters"]["snaps_synced"]
        self.assertEqual(snaps_synced, 1, f"Mismatch snaps_synced: {snaps_synced} vs 1")

        mode_local = self.mount_a.run_shell(["stat", "--format=%A", "l1"]).stdout.getvalue().strip()
        mode_remote = self.mount_b.run_shell(["stat", "--format=%A", "l1"]).stdout.getvalue().strip()

        self.assertTrue(mode_local == mode_remote, f"mode mismatch, local mode: {mode_local}, remote mode: {mode_remote}")

        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)
        self.mount_a.run_shell(["rmdir", "l1/.snap/snap0"])
        self.mount_a.run_shell(["rmdir", "l1"])

    def test_get_set_mirror_dirty_snap_id(self):
        """
        That get/set ceph.mirror.dirty_snap_id attribute succeeds in a remote filesystem.
        """
        log.debug('reconfigure client auth caps')
        self.get_ceph_cmd_result(
            'auth', 'caps', "client.{0}".format(self.mount_b.client_id),
                'mds', 'allow rw',
                'mon', 'allow r',
                'osd', 'allow rw pool={0}, allow rw pool={1}'.format(
                    self.backup_fs.get_data_pool_name(),
                    self.backup_fs.get_data_pool_name()))
        log.debug(f'mounting filesystem {self.secondary_fs_name}')
        self.mount_b.umount_wait()
        self.mount_b.mount_wait(cephfs_name=self.secondary_fs_name)
        log.debug('setting ceph.mirror.dirty_snap_id attribute')
        self.mount_b.run_shell(["mkdir", "-p", "d1/d2/d3"])
        attr = str(random.randint(1, 10))
        self.mount_b.setfattr("d1/d2/d3", "ceph.mirror.dirty_snap_id", attr)
        log.debug('getting ceph.mirror.dirty_snap_id attribute')
        val = self.mount_b.getfattr("d1/d2/d3", "ceph.mirror.dirty_snap_id")
        self.assertEqual(attr, val, f"Mismatch for ceph.mirror.dirty_snap_id value: {attr} vs {val}")

    def test_cephfs_mirror_remote_snap_corrupt_fails_synced_snapshot(self):
        """
        That making manual changes to the remote .snap directory shows 'peer status' state: "failed"
        for a synced snapshot and then restores to "idle" when those changes are reverted.
        """
        log.debug('reconfigure client auth caps')
        self.get_ceph_cmd_result(
            'auth', 'caps', "client.{0}".format(self.mount_b.client_id),
            'mds', 'allow rwps',
            'mon', 'allow r',
            'osd', 'allow rw pool={0}, allow rw pool={1}'.format(
                self.backup_fs.get_data_pool_name(),
                self.backup_fs.get_data_pool_name()))
        log.debug(f'mounting filesystem {self.secondary_fs_name}')
        self.mount_b.umount_wait()
        self.mount_b.mount_wait(cephfs_name=self.secondary_fs_name)

        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        peer_spec = "client.mirror_remote@ceph"
        self.peer_add(self.primary_fs_name, self.primary_fs_id, peer_spec, self.secondary_fs_name)
        dir_name = 'd0'
        self.mount_a.run_shell(['mkdir', dir_name])
        self.add_directory(self.primary_fs_name, self.primary_fs_id, f'/{dir_name}')

        # take a snapshot
        snap_name = "snap_a"
        expected_snap_count = 1
        self.mount_a.run_shell(['mkdir', f'{dir_name}/.snap/{snap_name}'])

        time.sleep(30)
        # confirm snapshot synced and status 'idle'
        self.check_peer_status_idle(self.primary_fs_name, self.primary_fs_id,
                                    peer_spec, f'/{dir_name}', snap_name, expected_snap_count)

        remote_snap_name = 'snap_b'
        remote_snap_path = f'{dir_name}/.snap/{remote_snap_name}'
        failure_reason = f"snapshot '{remote_snap_name}' has invalid metadata"
        dir_name = f'/{dir_name}'

        # create a directory in the remote fs and check status 'failed'
        self.mount_b.run_shell(['sudo', 'mkdir', remote_snap_path], omit_sudo=False)
        peer_uuid = self.get_peer_uuid(peer_spec)
        with safe_while(sleep=1, tries=60, action=f'wait for failed status: {peer_spec}') as proceed:
            while proceed():
                res = self.mirror_daemon_command(f'peer status for fs: {self.primary_fs_name}',
                                                 'fs', 'mirror', 'peer', 'status',
                                                 f'{self.primary_fs_name}@{self.primary_fs_id}', peer_uuid)
                if('failed' == res[dir_name]['state'] and \
                   failure_reason == res.get(dir_name, {}).get('failure_reason', {}) and \
                   snap_name == res[dir_name]['last_synced_snap']['name'] and \
                   expected_snap_count == res[dir_name]['snaps_synced']):
                    break
        # remove the directory in the remote fs and check status restores to 'idle'
        self.mount_b.run_shell(['sudo', 'rmdir', remote_snap_path], omit_sudo=False)
        with safe_while(sleep=1, tries=60, action=f'wait for idle status: {peer_spec}') as proceed:
            while proceed():
                res = self.mirror_daemon_command(f'peer status for fs: {self.primary_fs_name}',
                                                 'fs', 'mirror', 'peer', 'status',
                                                 f'{self.primary_fs_name}@{self.primary_fs_id}', peer_uuid)
                if('idle' == res[dir_name]['state'] and 'failure_reason' not in res and \
                   snap_name == res[dir_name]['last_synced_snap']['name'] and \
                   expected_snap_count == res[dir_name]['snaps_synced']):
                    break
        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)

    def test_cephfs_mirror_sync_already_existing_snapshots(self):
        """
        That mirroring syncs the already existing snapshot correctly.
        """
        log.debug('reconfigure client auth caps')
        self.get_ceph_cmd_result(
            'auth', 'caps', "client.{0}".format(self.mount_b.client_id),
                'mds', 'allow rw',
                'mon', 'allow r',
                'osd', 'allow rw pool={0}, allow rw pool={1}'.format(
                    self.backup_fs.get_data_pool_name(),
                    self.backup_fs.get_data_pool_name()))

        log.debug(f'mounting filesystem {self.secondary_fs_name}')
        self.mount_b.umount_wait()
        self.mount_b.mount_wait(cephfs_name=self.secondary_fs_name)

        self.enable_mirroring(self.primary_fs_name, self.primary_fs_id)
        peer_spec = "client.mirror_remote@ceph"
        self.peer_add(self.primary_fs_name, self.primary_fs_id, peer_spec, self.secondary_fs_name)

        dir_name = 'dir'

        # make some change in the fs and take a snapshot
        snap_a = "snap_a"
        self.mount_a.run_shell(['mkdir', '-p', f'{dir_name}/d1'])
        self.mount_a.write_n_mb(os.path.join(f'{dir_name}/d1', 'file1'), 1)
        self.mount_a.run_shell(['mkdir', f'{dir_name}/.snap/{snap_a}'])

        # make some more changes in the fs and take another snapshot
        snap_b = "snap_b"
        self.mount_a.write_n_mb(os.path.join(f'{dir_name}/d1', 'file2'), 1)
        self.mount_a.run_shell(['mkdir', f'{dir_name}/.snap/{snap_b}'])

        # make another change in the fs and don't take snapshot
        self.mount_a.run_shell(['rm', f'{dir_name}/d1/file2'])

        # add the directory for mirroring
        self.add_directory(self.primary_fs_name, self.primary_fs_id, f'/{dir_name}')

        time.sleep(60)

        # confirm snapshot synced and status 'idle'
        expected_snap_count = 2
        self.check_peer_status_idle(self.primary_fs_name, self.primary_fs_id,
                                    peer_spec, f'/{dir_name}', snap_b, expected_snap_count)
        self.verify_snapshot(dir_name, snap_a)
        self.verify_snapshot(dir_name, snap_b)
        self.disable_mirroring(self.primary_fs_name, self.primary_fs_id)


class TestPeerWriterMirroring(CephFSTestCase):
    MDSS_REQUIRED = 1
    CLIENTS_REQUIRED = 0
    REQUIRE_FILESYSTEM = False

    MODULE_NAME = 'mirroring'

    def run(self, result=None):
        try:
            return super(TestPeerWriterMirroring, self).run(result)
        except KeyboardInterrupt:
            try:
                self._shutdown_peer_writer()
            except Exception:
                pw_log.exception(
                    'PEER_WRITER_TEST interrupt cleanup failed')
            raise

    def setUp(self):
        self.peer_writer_env = None
        self.mirroring_module_enabled = False
        super(TestPeerWriterMirroring, self).setUp()
        self.run_ceph_cmd('mgr', 'module', 'enable', self.MODULE_NAME)
        self.mirroring_module_enabled = True

    def tearDown(self):
        errors = self._shutdown_peer_writer()
        try:
            super(TestPeerWriterMirroring, self).tearDown()
        except Exception as error:
            errors.append(error)
        if errors:
            raise errors[0]

    def _shutdown_peer_writer(self):
        errors = []
        if self.mirroring_module_enabled:
            try:
                self.run_ceph_cmd('mgr', 'module', 'disable',
                                  self.MODULE_NAME)
                self.mirroring_module_enabled = False
            except Exception as error:
                errors.append(error)
        if self.peer_writer_env is not None and \
           not self.mirroring_module_enabled:
            try:
                self.peer_writer_env.close()
                self.peer_writer_env = None
            except Exception as error:
                errors.append(error)
        return errors

    def create_env(self, topology=None, daemon_count=4):
        self.assertIsNone(self.peer_writer_env)
        self.peer_writer_env = _PeerWriterBalancerEnv(self, topology, daemon_count)
        return self.peer_writer_env

    def test_peer_writer_directory_add_remove_with_delayed_acks(self):
        env = self.create_env({'fs0': {'peer00': 1}}, daemon_count=1)
        env.populate()
        daemon = env.daemons['daemon.0']
        peer_uuid = env.peer_uuid('peer00')
        key = (peer_uuid, '/peer00/delayed')
        directory_id = env.directory_id(*key)
        baseline = copy.deepcopy(env.load_state('fs0')[1])

        def maps(label):
            memory = {(item['peer_uuid'], item['path']): item
                      for item in env.list_directories('fs0')}
            _, persisted, epochs = env.load_state('fs0')
            self.assertEqual(set(memory), set(persisted))
            for directory, record in persisted.items():
                for field in ('instance_id', 'assignment_epoch', 'purging'):
                    self.assertEqual(memory[directory][field], record[field])
                self.assertEqual(record['assignment_epoch'], epochs[directory])
            for directory, record in baseline.items():
                self.assertEqual(persisted[directory], record)
                self.assertEqual(memory[directory]['state'], 'assigned')
            daemon.flush_callback_logs()
            pw_log.info('PEER_WRITER_TEST delayed ACK %s dir_map=%s '
                        'rados_dir_map=%s epochs=%s', label, memory, persisted, epochs)
            return memory, persisted, epochs

        def ready(state):
            memory = {(item['peer_uuid'], item['path']): item
                      for item in env.list_directories('fs0')}
            return key not in memory if state is None else \
                memory.get(key, {}).get('state') == state

        memory, persisted, epochs = maps('before add')
        self.assertNotIn(key, memory)
        self.assertNotIn(key, persisted)
        self.assertNotIn(key, epochs)
        try:
            acquire_gate = daemon.delay_ack('acquire', *key)
            self.run_ceph_cmd('fs', 'snapshot', 'mirror', 'peer_writer', 'add',
                              'fs0', *key)
            env.all_dirs.append(directory_id)
            acquire = acquire_gate.wait_received()
            memory, persisted, epochs = maps('before acquire ACK')
            self.assertEqual(memory[key]['state'], 'acquiring')
            self.assertFalse(memory[key]['purging'])
            self.assertEqual(memory[key]['instance_id'], daemon.instance_id)
            self.assertEqual(epochs[key], 1)
            self.assertEqual(acquire['assignment_epoch'], epochs[key])
            self.assertNotIn(directory_id, daemon.acquired)
            assigned = copy.deepcopy(persisted[key])
            daemon.release_delayed_acks()
            env.wait_until(lambda: ready('assigned'))
            memory, persisted, epochs = maps('after acquire ACK')
            self.assertEqual(memory[key]['state'], 'assigned')
            self.assertEqual(persisted[key], assigned)
            self.assertIn(directory_id, daemon.acquired)
            self.assertFalse(acquire_gate.timed_out)

            release_gate = daemon.delay_ack('release', *key)
            self.run_ceph_cmd('fs', 'snapshot', 'mirror', 'peer_writer', 'remove',
                              'fs0', *key)
            release = release_gate.wait_received()
            memory, persisted, epochs = maps('before release ACK')
            self.assertEqual(memory[key]['state'], 'releasing')
            self.assertTrue(memory[key]['purging'])
            self.assertEqual(persisted[key], dict(assigned, purging=True))
            self.assertEqual(release['assignment_epoch'], assigned['assignment_epoch'])
            self.assertNotIn(directory_id, daemon.released)
            daemon.release_delayed_acks()
            env.wait_until(lambda: ready(None))
            env.all_dirs.remove(directory_id)
            memory, persisted, epochs = maps('after release ACK')
            self.assertNotIn(key, memory)
            self.assertNotIn(key, persisted)
            self.assertEqual(epochs[key], assigned['assignment_epoch'])
            self.assertIn(directory_id, daemon.released)
            self.assertFalse(release_gate.timed_out)
        finally:
            daemon.release_delayed_acks()

    def test_global_peer_writer_directory_balancing(self):
        env = self.create_env()
        env.populate()

        self.assertEqual(env.assigned_directory_count(), 36)
        self.assertEqual(env.unassigned_directory_count(), 0)
        env.assert_unique_assignment(self)
        env.assert_rados_matches_policy(self)

        expected_peer_distributions = {
            'peer00': [1, 2, 2, 2],
            'peer01': [1, 1, 1, 2],
            'peer10': [1, 1, 2, 2],
            'peer11': [1, 2, 2, 2],
            'peer20': [1, 1, 1, 2],
            'peer21': [1, 1, 2, 2],
        }
        for peer_uuid, expected in expected_peer_distributions.items():
            dist = env.peer_distribution(peer_uuid)
            self.assertLessEqual(max(dist) - min(dist), 1)
            self.assertEqual(sorted(dist), expected)
            rados_dist = env.rados_peer_distribution(self, peer_uuid)
            self.assertLessEqual(max(rados_dist) - min(rados_dist), 1)
            self.assertEqual(sorted(rados_dist), expected)

        self.assertEqual(sorted(env.fs_distribution('fs0')), [3, 3, 3, 3])
        self.assertEqual(sorted(env.fs_distribution('fs1')), [3, 3, 3, 4])
        self.assertEqual(sorted(env.fs_distribution('fs2')), [2, 3, 3, 3])
        self.assertEqual(sorted(env.total_distribution()), [9, 9, 9, 9])
        self.assertEqual(sorted(env.rados_fs_distribution(self, 'fs0')),
                         [3, 3, 3, 3])
        self.assertEqual(sorted(env.rados_fs_distribution(self, 'fs1')),
                         [3, 3, 3, 4])
        self.assertEqual(sorted(env.rados_fs_distribution(self, 'fs2')),
                         [2, 3, 3, 3])
        self.assertEqual(sorted(env.rados_total_distribution(self)),
                         [9, 9, 9, 9])

        owners = env.owners()
        for directory, daemon_id in owners.items():
            self.assertIn(directory, env.get_daemon_dirs(daemon_id))
            self.assertIn(directory, env.daemons[daemon_id].acquired)
        for fs_name, peers in env.topology.items():
            _, directories, epochs = env.load_state(fs_name)
            self.assertEqual(len(directories), sum(peers.values()))
            self.assertEqual(len(epochs), len(directories))

    def test_global_peer_writer_directory_balancer_tie_breaking(self):
        env = self.create_env({'fs0': {'peer00': 1}})
        env.populate()

        directory = env.all_dirs[0]
        self.assertEqual(env.owners(), {directory: 'daemon.0'})
        rados_owners, _ = env.rados_owners_and_epochs(self)
        self.assertEqual(rados_owners, {directory: 'daemon.0'})
        pw_log.info('PEER_WRITER_TEST tie selected daemon.0 for %s',
                    directory)

    def test_global_peer_writer_directory_balancing_after_daemon_failure(self):
        env = self.create_env()
        env.populate()
        self.assertEqual(sorted(env.total_distribution()), [9, 9, 9, 9])
        owners_before = env.owners()
        persisted_before = env.rados_owners_and_epochs(self)
        acquire_counts = {daemon_id: len(daemon.acquired)
                          for daemon_id, daemon in env.daemons.items()}
        failed_dirs = env.get_daemon_dirs('daemon.0')
        self.assertEqual(len(failed_dirs), 9)

        env.stop_daemon('daemon.0')

        self.assertEqual(env.get_daemon_dirs('daemon.0'), failed_dirs)
        env.assert_unique_assignment(self)
        env.assert_rados_matches_policy(self)
        self.assertEqual(env.owners(), owners_before)
        self.assertEqual(env.rados_owners_and_epochs(self), persisted_before)
        self.assertEqual(env.daemons['daemon.0'].released, [])
        self.assertEqual({daemon_id: len(daemon.acquired)
                          for daemon_id, daemon in env.daemons.items()}, acquire_counts)
        records = env.directory_records()
        states = Counter(item['state'] for items in records.values() for item in items)
        self.assertEqual(states, {'fencing': 9, 'assigned': 27})

    def test_global_peer_writer_directory_balancing_after_mgr_module_restart(self):
        env = self.create_env()
        env.populate()
        owners_before = env.owners()
        acquire_counts = {daemon_id: len(daemon.acquired)
                          for daemon_id, daemon in env.daemons.items()}
        release_counts = {daemon_id: len(daemon.released)
                          for daemon_id, daemon in env.daemons.items()}

        env.restart_mgr_module()

        self.assertEqual(env.assigned_directory_count(), 36)
        env.assert_unique_assignment(self)
        env.assert_rados_matches_policy(self)
        self.assertEqual(sorted(env.total_distribution()), [9, 9, 9, 9])
        self.assertEqual(sorted(env.rados_total_distribution(self)),
                         [9, 9, 9, 9])
        self.assertEqual(env.owners(), owners_before)
        self.assertEqual({daemon_id: len(daemon.acquired)
                          for daemon_id, daemon in env.daemons.items()},
                         acquire_counts)
        self.assertEqual({daemon_id: len(daemon.released)
                          for daemon_id, daemon in env.daemons.items()},
                         release_counts)
