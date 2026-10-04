import base64
import copy
import errno
import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

os.environ['UNITTEST'] = 'true'
import tests  # noqa: F401  # pylint: disable=unused-import,wrong-import-position

from mirroring.fs import peer_writer, writer_notify, writer_state
from mirroring.fs.exception import MirrorException
from mirroring.fs.peer_writer import PeerWriterControlPlane, PeerWriterPolicy


class FakeWriterState:
    def __init__(self):
        self.calls = []

    def update_directory(self, peer_uuid, path, record, epoch=None):
        self.calls.append((peer_uuid, path, record, epoch))


def make_policy():
    return PeerWriterPolicy(Mock(), Mock(), 'dstfs', FakeWriterState())


class TestPeerWriterControlPlane(unittest.TestCase):
    def test_peer_writer_peer_add_stores_secret_and_publishes_non_secret(self):
        peer_uuid = '11111111-1111-1111-1111-111111111111'
        issued = []

        def mon_command(cmd):
            issued.append(cmd)
            if cmd['prefix'] == 'config-key get':
                return -errno.ENOENT, '', ''
            if cmd['prefix'] == 'config-key set':
                return 0, '', ''
            if cmd['prefix'] == 'fs mirror peer_writer peer_list':
                return 0, json.dumps({}), ''
            if cmd['prefix'] == 'fs mirror peer_writer peer_add':
                return 0, '', ''
            raise AssertionError(f'unexpected command {cmd}')

        mgr = Mock()
        mgr.mon_command.side_effect = mon_command

        control = PeerWriterControlPlane.__new__(PeerWriterControlPlane)
        control.mgr = mgr
        control.rados = Mock()
        control.snapshot_mirror = Mock()
        control.fs_map = {
            'filesystems': [{
                'id': 7,
                'mdsmap': {
                    'fs_name': 'dstfs',
                    'metadata_pool': 23,
                },
            }]
        }
        control.lock = threading.Lock()
        control.policies = {}
        control.peers = {}
        control.stopping = False
        control._verify_source = Mock()
        control._open_policy = Mock(return_value=Mock())

        r, out, err = control.peer_add(
            'dstfs', peer_uuid, 'client.writer@srcsite', 'srcfs',
            {'mon_host': '1.2.3.4:6789', 'key': 'fake-key'})

        self.assertEqual((r, json.loads(out), err), (0, {}, ''))
        control._verify_source.assert_called_once_with(
            'dstfs', peer_uuid, 'client.writer@srcsite', 'srcfs',
            {'mon_host': '1.2.3.4:6789', 'key': 'fake-key'})
        self.assertIn({
            'prefix': 'config-key set',
            'key': f'cephfs/mirror/peer_writer/dstfs/{peer_uuid}',
            'val': json.dumps({'mon_host': '1.2.3.4:6789',
                               'key': 'fake-key'}, sort_keys=True),
        }, issued)
        self.assertIn({
            'prefix': 'fs mirror peer_writer peer_add',
            'fs_name': 'dstfs',
            'uuid': peer_uuid,
            'source_cluster_spec': 'client.writer@srcsite',
            'source_fs_name': 'srcfs',
        }, issued)

    def test_peer_writer_peer_remove_rejects_referenced_peer(self):
        peer_uuid = '11111111-1111-1111-1111-111111111111'
        control = PeerWriterControlPlane.__new__(PeerWriterControlPlane)
        control.lock = threading.Lock()
        control.peers = {}
        control.fs_map = {
            'filesystems': [{'mdsmap': {'fs_name': 'dstfs'}}]
        }
        control._open_policy = Mock()
        control._open_policy.return_value.references_peer.return_value = True

        r, out, err = control.peer_remove('dstfs', peer_uuid)

        self.assertEqual((r, out), (-errno.EBUSY, ''))
        self.assertIn('still has destination directories', err)

    def test_peer_writer_peer_remove_removes_monitor_record_and_secret(self):
        peer_uuid = '11111111-1111-1111-1111-111111111111'
        issued = []

        def mon_command(cmd):
            issued.append(cmd)
            return 0, '', ''

        control = PeerWriterControlPlane.__new__(PeerWriterControlPlane)
        control.mgr = Mock()
        control.mgr.mon_command.side_effect = mon_command
        control.lock = threading.Lock()
        control.peers = {('dstfs', peer_uuid): {}}
        control.fs_map = {
            'filesystems': [{'mdsmap': {'fs_name': 'dstfs'}}]
        }
        control._open_policy = Mock()
        control._open_policy.return_value.references_peer.return_value = False

        r, out, err = control.peer_remove('dstfs', peer_uuid)

        self.assertEqual((r, json.loads(out), err), (0, {}, ''))
        self.assertIn({
            'prefix': 'fs mirror peer_writer peer_remove',
            'fs_name': 'dstfs',
            'uuid': peer_uuid,
        }, issued)
        self.assertIn({
            'prefix': 'config-key rm',
            'key': f'cephfs/mirror/peer_writer/dstfs/{peer_uuid}',
        }, issued)


class TestPeerWriterPolicy(unittest.TestCase):
    def test_directory_add_and_remove_unassigned_updates_state(self):
        peer_uuid = '11111111-1111-1111-1111-111111111111'
        policy = make_policy()

        policy.add_directory(peer_uuid, '/data')
        policy.remove_directory(peer_uuid, '/data')

        self.assertNotIn((peer_uuid, '/data'), policy.directories)
        self.assertEqual(policy.state.calls[0][0:2], (peer_uuid, '/data'))
        self.assertEqual(policy.state.calls[0][2]['instance_id'], '')
        self.assertEqual(policy.state.calls[0][3], 0)
        self.assertEqual(policy.state.calls[-1], (peer_uuid, '/data', None, 0))

    def test_directory_add_rejects_duplicate_ancestor_and_subtree(self):
        policy = make_policy()
        policy.add_directory('peer-a', '/data/subdir')

        with self.assertRaisesRegex(Exception, 'already tracked'):
            policy.add_directory('peer-b', '/data/subdir')
        with self.assertRaisesRegex(Exception, 'conflicts with tracked path'):
            policy.add_directory('peer-b', '/data')
        with self.assertRaisesRegex(Exception, 'conflicts with tracked path'):
            policy.add_directory('peer-b', '/data/subdir/child')

        policy.add_directory('peer-b', '/other')
        self.assertIn(('peer-b', '/other'), policy.directories)


PEER = '11111111-1111-1111-1111-111111111111'
KEY = (PEER, '/data')


def writer(daemon='a', filesystems=('dstfs',), **changes):
    record = dict(addr='addr-' + daemon, daemon_id=daemon,
                  process_incarnation='inc-' + daemon,
                  destination_client_identity='client-' + daemon,
                  protocol_version=1, filesystems=list(filesystems),
                  features=sorted(PeerWriterPolicy.REQUIRED_FEATURES))
    record.update(changes)
    return record


def directory(epoch=1):
    return dict(version=1, instance_id='42', assignment_epoch=epoch,
                last_shuffled=100.0, purging=False, reassigning=False,
                process_incarnation='inc-a', destination_client_identity='client-a')


def live_policy(filesystem='dstfs', balancer=None):
    policy = PeerWriterPolicy(Mock(), Mock(), filesystem, Mock(stale=False), balancer)
    policy._notify = Mock()
    policy._schedule_retry_locked = Mock()
    return policy


def assign(policy, key=KEY):
    policy.add_directory(*key)
    message = policy._notify.call_args.args[2]
    policy._handle_acquire(key, message['operation_id'], 0)
    return message


def control_plane():
    control = PeerWriterControlPlane.__new__(PeerWriterControlPlane)
    control.mgr = Mock()
    control.lock = threading.Lock()
    control.fs_map = {'filesystems': [{'mdsmap': {'fs_name': 'dstfs'}}]}
    control.peers = {}
    control._peer_list = Mock(return_value={})
    control._config_get = Mock(return_value={})
    control._config_set = Mock()
    control._verify_source = Mock()
    control._open_policy = Mock()
    return control


class TestPeerWriterPeerEdges(unittest.TestCase):
    def test_input_validation_and_bootstrap(self):
        control = control_plane()
        for peer, conf in [('bad-uuid', {}), (PEER, {'key': 'k'}),
                           (PEER, {'mon_host': 'm'})]:
            self.assertEqual(control.peer_add('dstfs', peer, 'c@s', 'src', conf)[0],
                             -errno.EINVAL)
        control._verify_source.assert_not_called()
        token = dict(peer_uuid=PEER, fsid='fsid', filesystem='src', user='c',
                     site_name='s', key='k', mon_host='m')
        with patch.object(control, 'peer_add', return_value=(0, '{}', '')) as add:
            encoded = base64.b64encode(json.dumps(token).encode()).decode()
            self.assertEqual(control.peer_bootstrap_import('dstfs', encoded)[0], 0)
            add.assert_called_once_with('dstfs', PEER, 'c@s', 'src',
                                        {'fsid': 'fsid', 'key': 'k', 'mon_host': 'm'})
            self.assertEqual(control.peer_bootstrap_import('dstfs', 'bad!')[0],
                             -errno.EINVAL)

    def test_repeated_peer_add_is_idempotent_but_conflicts_fail(self):
        control = control_plane()
        control._peer_list.return_value = {
            PEER: {'source_cluster_spec': 'c@s', 'source_fs_name': 'src'}}
        self.assertEqual(control.peer_add('dstfs', PEER, 'c@s', 'src', {})[0], 0)
        self.assertEqual(control.peer_add('dstfs', PEER, 'other@s', 'src', {})[0],
                         -errno.EEXIST)
        control._verify_source.assert_not_called()
        control._config_set.assert_not_called()
        control.mgr.mon_command.assert_not_called()

    def test_failed_peer_add_rolls_back_only_confirmed_absent_relationship(self):
        matching = {PEER: {'source_cluster_spec': 'c@s', 'source_fs_name': 'src'}}
        for lookup, result, rollback in [({}, -errno.ETIMEDOUT, True),
                                        (matching, 0, False),
                                        (MirrorException(-errno.EIO),
                                         -errno.ETIMEDOUT, False)]:
            with self.subTest(result=result, rollback=rollback):
                control = control_plane()
                control._peer_list.side_effect = [{}, lookup]
                control.mgr.mon_command.return_value = (-errno.ETIMEDOUT, '', 'timeout')
                conf = {'mon_host': 'm', 'key': 'k'}
                self.assertEqual(control.peer_add('dstfs', PEER, 'c@s', 'src', conf)[0],
                                 result)
                key = control.peer_config_key('dstfs', PEER)
                expected = [call(key, conf)] + ([call(key)] if rollback else [])
                self.assertEqual(control._config_set.call_args_list, expected)


class RadosError(Exception):
    def __init__(self, error):
        super().__init__('injected RADOS error')
        self.errno = error


class OmapOperation:
    def __init__(self):
        self.actions = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def new(self, flags):
        self.actions.append(('new',))

    def omap_cmp(self, key, value, comparison):
        self.actions.append(('compare', key, value))


class OmapIoctx:
    """Deferred reads and atomic staged writes for the one writer-state object."""
    def __init__(self):
        self.data = None

    def get_omap_vals_by_keys(self, op, keys):
        rows = []
        op.actions.append(('read', keys, rows))
        return rows, 0

    def get_omap_vals(self, op, start, prefix, limit):
        keys = sorted(key for key in self.data if key > start and key.startswith(prefix))
        return self.get_omap_vals_by_keys(op, keys[:limit])

    def operate_read_op(self, op, oid):
        for _, keys, rows in op.actions:
            rows.extend((key, self.data[key]) for key in keys if key in self.data)

    def set_omap(self, op, keys, values):
        op.actions.append(('set', dict(zip(keys, values))))

    def remove_omap_keys(self, op, keys):
        op.actions.append(('remove', keys))

    def operate_write_op(self, op, oid):
        staged = dict(self.data or {})
        for action in op.actions:
            if action[0] == 'new' and self.data is not None:
                raise RadosError(errno.EEXIST)
            if action[0] == 'compare' and staged.get(action[1]) != action[2].encode():
                raise RadosError(errno.ECANCELED)
            if action[0] == 'set':
                staged.update({key: value.encode() if isinstance(value, str) else value
                               for key, value in action[1].items()})
            if action[0] == 'remove':
                for key in action[1]:
                    staged.pop(key, None)
        self.data = staged


def omap_binding():
    return SimpleNamespace(Error=RadosError, WriteOpCtx=OmapOperation,
                           ReadOpCtx=OmapOperation,
                           LIBRADOS_CREATE_EXCLUSIVE=1, LIBRADOS_CMPXATTR_OP_EQ=1)


class TestPeerWriterState(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(writer_state, 'rados', omap_binding())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ioctx = OmapIoctx()
        self.state = writer_state.WriterState(self.ioctx)
        self.state.initialize()

    def test_roundtrip_and_removal_retain_epoch_floor(self):
        instance = PeerWriterPolicy._persistent_instance(writer())
        self.state.update_instance('42', instance)
        self.state.update_directory(*KEY, directory(7), 7)
        self.assertEqual(self.state.load(), ({'42': instance}, {KEY: directory(7)}, {KEY: 7}))
        self.state.update_directory(*KEY, None, 7)
        self.state.update_instance('42', None)
        self.assertEqual(self.state.load(), ({}, {}, {KEY: 7}))
        self.state.initialize()  # Existing state must not reset its revision or epochs.
        self.assertEqual(self.state.load()[2], {KEY: 7})

    def test_corruption_and_invalid_numeric_types_are_rejected(self):
        self.state.update_directory(*KEY, directory(), 1)
        original = dict(self.ioctx.data)
        cases = [(writer_state.SCHEMA_KEY, b'{', -errno.EUCLEAN),
                 (writer_state.SCHEMA_KEY,
                  writer_state.encode_json({'format': writer_state.WRITER_OBJECT_NAME,
                                            'version': True}), -errno.EOPNOTSUPP),
                 (self.state.directory_key(*KEY),
                  writer_state.encode_json(dict(directory(), assignment_epoch=True)),
                  -errno.EUCLEAN),
                 (self.state.epoch_key(*KEY), b'2', -errno.EUCLEAN)]
        for key, value, error in cases:
            with self.subTest(key=key, error=error):
                self.ioctx.data = dict(original, **{key: value})
                with self.assertRaises(MirrorException) as raised:
                    self.state.load()
                self.assertEqual(raised.exception.args[0], error)

    def test_load_paginates_directories_and_epochs(self):
        expected = {}
        for index in range(writer_state.MAX_RETURN + 1):
            key = (PEER, '/dir-' + str(index).zfill(4))
            expected[key] = directory()
            self.state.update_directory(*key, expected[key], 1)
        _, directories, epochs = self.state.load()
        self.assertEqual(directories, expected)
        self.assertEqual(epochs, dict.fromkeys(expected, 1))

    def test_cas_conflict_is_atomic_and_reload_allows_retry(self):
        self.ioctx.data[writer_state.REVISION_KEY] = b'1'  # Another manager committed.
        before = dict(self.ioctx.data)
        with self.assertRaises(MirrorException) as raised:
            self.state.update_directory(*KEY, directory(), 1)
        self.assertEqual(raised.exception.args[0], -errno.EAGAIN)
        self.assertTrue(self.state.stale)
        self.assertEqual(self.ioctx.data, before)
        self.state.load()
        self.state.update_directory(*KEY, directory(), 1)
        self.assertFalse(self.state.stale)
        self.assertEqual(self.state.revision, 2)


def ack(message, **changes):
    response = dict(message, result=0, quiesced=True)
    response.update(changes)
    return ('42', 1, json.dumps(response).encode())


class TestPeerWriterNotifications(unittest.TestCase):
    def test_ack_identity_types_and_quiesced_release(self):
        message = PeerWriterPolicy._message('acquire', KEY, directory())
        self.assertEqual(writer_notify.WriterNotifier._decode_ack(ack(message), message), 0)
        for field, value in [('operation_id', 'old'), ('peer_uuid', 'other'),
                             ('path', '/other'), ('process_incarnation', 'old'),
                             ('version', True), ('assignment_epoch', 1.0)]:
            with self.subTest(field=field):
                self.assertIsNone(writer_notify.WriterNotifier._decode_ack(
                    ack(message, **{field: value}), message))
        self.assertEqual(writer_notify.WriterNotifier._decode_ack(
            ack(message, result=False), message), -errno.EPROTO)
        message['mode'] = 'release'
        self.assertEqual(writer_notify.WriterNotifier._decode_ack(
            ack(message, quiesced=False), message), -errno.EBUSY)
        self.assertEqual(writer_notify.WriterNotifier._decode_ack(ack(message), message), 0)

    def test_notification_errors_and_matching_completion(self):
        ioctx, complete = Mock(), Mock()
        notifier = writer_notify.WriterNotifier(ioctx)
        message = PeerWriterPolicy._message('acquire', KEY, directory())
        for result, replies, timeouts, expected in [
                (0, [ack(message)], [], 0), (-errno.EIO, [ack(message)], [], -errno.EIO),
                (0, [], [('42', 1)], -errno.ETIMEDOUT), (0, [], [], -errno.EPROTO)]:
            complete.reset_mock()
            notifier.notify('42', message, complete)
            ioctx.aio_notify.call_args.args[1](None, result, replies, timeouts)
            complete.assert_called_once_with(expected)
        complete.reset_mock()
        with patch.object(writer_notify, 'rados', SimpleNamespace(Error=RadosError)):
            ioctx.aio_notify.side_effect = RadosError(errno.EIO)
            notifier.notify('42', message, complete)
        complete.assert_called_once_with(-errno.EIO)

    def test_discovery_validation_expiry_and_stop(self):
        ioctx, listener = Mock(), Mock()
        with patch.object(writer_notify.threading, 'Timer') as timer, \
                patch.object(writer_notify.time, 'time', return_value=100) as clock:
            watcher = writer_notify.WriterInstanceWatcher(ioctx, listener)
            record = dict(writer(), version=1)
            valid = ('42', 1, json.dumps(record).encode())
            invalid = ('43', 1, json.dumps(dict(record, features=[{}])).encode())
            watcher.notify()
            with self.assertLogs(writer_notify.log, level='WARNING'):
                ioctx.aio_notify.call_args.args[1](None, 0, [invalid, valid], [])
            self.assertEqual(set(listener.call_args.args[0]), {'42'})
            clock.return_value = 130
            watcher.notify()
            ioctx.aio_notify.call_args.args[1](None, -errno.EIO, [], [])
            self.assertIn('42', watcher.instances)  # Expiry is strictly greater than 30s.
            clock.return_value = 131
            watcher.notify()
            ioctx.aio_notify.call_args.args[1](None, 0, [], [])
            self.assertEqual(set(listener.call_args.args[1]), {'42'})
            self.assertEqual(watcher.callbacks, 0)
            watcher.stop()
            timer.return_value.cancel.assert_called_once_with()
            timer.return_value.join.assert_called_once_with()
            calls = ioctx.aio_notify.call_count
            watcher.notify()
            self.assertEqual(ioctx.aio_notify.call_count, calls)


class TestPeerWriterPolicyEdges(unittest.TestCase):
    def test_weighted_placement_and_concurrent_filesystem_batches(self):
        balancer = PeerWriterPolicy.DirectoryBalancer()
        candidates = {'42': writer('a'), '43': writer('b')}
        self.assertEqual(balancer.choose('dstfs', PEER, candidates), '42')
        balancer.register('dstfs', PEER, '/a', '42', 'a')
        balancer.register('otherfs', PEER, '/b', '43', 'b')
        self.assertEqual(balancer.choose('dstfs', PEER, candidates), '43')
        balancer.unregister('dstfs', PEER, '/a')
        balancer.unregister('otherfs', PEER, '/b')
        topology = {'fs0': (7, 5), 'fs1': (6, 7), 'fs2': (5, 6)}
        instances = {str(i): writer(str(i), topology) for i in range(4)}
        policies, errors = [], []
        barrier = threading.Barrier(3)
        for fs, counts in topology.items():
            policy = live_policy(fs, balancer)
            for peer, count in enumerate(counts):
                for index in range(count):
                    policy.add_directory(str(peer), f'/peer{peer}/dir{index}')
            policies.append(policy)

        def discover(policy):
            try:
                barrier.wait(timeout=2)
                policy.update_instances(instances, {})
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=discover, args=(p,), daemon=True) for p in policies]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(balancer.total_counts.values()), [9, 9, 9, 9])
        self.assertEqual(len(balancer.owners), 36)

    def test_assignment_persists_before_notify_and_failures_preserve_epoch(self):
        policy = live_policy()
        policy.update_instances({'42': writer()}, {})

        def notified(instance, key, message):
            self.assertEqual(policy.state.update_directory.call_args.args,
                             (key[0], key[1], policy.directories[key], 1))
            self.assertEqual(policy.directory_balancer.total_counts, {'a': 1})

        policy._notify.side_effect = notified
        assign(policy)
        policy._notify.reset_mock(side_effect=True)
        policy.state.update_directory.side_effect = MirrorException(-errno.EIO, 'write failed')
        policy.directories[(PEER, '/other')] = dict(directory(0), instance_id='')
        with policy.lock:
            self.assertEqual(policy._assign_unassigned_locked(), [])
        self.assertEqual(policy.directories[(PEER, '/other')]['instance_id'], '')
        self.assertNotIn((PEER, '/other'), policy.epochs)
        policy.state.update_directory.side_effect = None
        policy.epochs[(PEER, '/other')] = writer_state.MAX_EPOCH
        with policy.lock:
            self.assertEqual(policy._assign_unassigned_locked(), [])
        self.assertIn('exhausted', policy.operation_status[(PEER, '/other')]['reason'])

    def test_acquire_retry_ignores_stale_completion(self):
        policy = live_policy()
        policy.update_instances({'42': writer()}, {})
        policy.add_directory(*KEY)
        old = policy._notify.call_args.args[2]
        policy._handle_acquire(KEY, old['operation_id'], -errno.EIO)
        policy._retry()
        current = policy._notify.call_args.args[2]
        self.assertNotEqual(old['operation_id'], current['operation_id'])
        self.assertEqual(current['assignment_epoch'], old['assignment_epoch'])
        policy._handle_acquire(KEY, old['operation_id'], 0)
        self.assertEqual(policy.operation_status[KEY]['state'], 'acquiring')
        policy._handle_acquire(KEY, current['operation_id'], 0)
        self.assertEqual(policy.operation_status[KEY]['state'], 'assigned')

    def test_fencing_release_handoff_and_purge_retain_epochs(self):
        for changes in ({'process_incarnation': 'new'}, {'features': []}):
            policy = live_policy()
            policy.update_instances({'42': writer()}, {})
            assign(policy)
            before = copy.deepcopy(policy.directories[KEY])
            changed = writer(**changes)
            policy.update_instances({'42': changed}, {})
            self.assertEqual(policy.operation_status[KEY]['state'], 'fencing')
            if 'features' in changes:
                policy.update_instances({}, {'42': changed})
                self.assertEqual(policy.operation_status[KEY]['state'], 'fencing')
            self.assertEqual(policy.directories[KEY], before)
        policy = live_policy()
        policy.update_instances({'42': writer()}, {})
        assign(policy)
        policy.update_instances({'43': writer('b')}, {})
        policy.update_instances({'42': writer(filesystems=('otherfs',))}, {})
        release = policy._notify.call_args.args[2]
        self.assertEqual(release['mode'], 'release')
        self.assertEqual(policy.directories[KEY]['instance_id'], '42')
        policy._handle_release(KEY, release['operation_id'], 0)
        acquire = policy._notify.call_args.args[2]
        self.assertEqual((acquire['mode'], acquire['instance_id'], acquire['assignment_epoch']),
                         ('acquire', '43', 2))
        policy._handle_acquire(KEY, acquire['operation_id'], 0)
        policy.remove_directory(*KEY)
        release = policy._notify.call_args.args[2]
        policy._handle_release(KEY, release['operation_id'], 0)
        self.assertNotIn(KEY, policy.directories)
        self.assertEqual(policy.epochs[KEY], 2)

    def test_reload_rebalance_and_shutdown_bookkeeping(self):
        policy = live_policy()
        policy.update_instances({'42': writer()}, {})
        assign(policy)
        policy.state.load.return_value = ({}, {}, {KEY: 1})
        with policy.lock:
            policy._reload_state_locked()
        self.assertEqual(policy.directory_balancer.owners, {})
        self.assertEqual(policy.directory_balancer.total_counts, {})
        policy = live_policy()
        policy.update_instances({'42': writer()}, {})
        for index in range(8):
            assign(policy, (PEER, '/dir-' + str(index)))
        policy.update_instances({'43': writer('b')}, {})
        with patch('mirroring.fs.peer_writer.time.time',
                   return_value=max(r['last_shuffled'] for r in policy.directories.values()) +
                   policy.SHUFFLE_INTERVAL):
            policy._retry()
            self.assertEqual(policy._notify.call_count, 12)
            policy._retry()
            self.assertEqual(policy._notify.call_count, 12)
        events = Mock()
        policy.retry_task, policy.watcher, policy.finisher = Mock(), Mock(), Mock()
        for name, dependency in [('timer', policy.retry_task), ('watcher', policy.watcher),
                                 ('tracker', policy.op_tracker), ('finisher', policy.finisher),
                                 ('ioctx', policy.ioctx)]:
            if name == 'tracker':
                dependency.wait_for_ops = Mock()
            events.attach_mock(dependency if name != 'tracker' else dependency.wait_for_ops,
                               name)
        policy.shutdown()
        self.assertEqual(events.mock_calls, [call.timer.cancel(), call.timer.join(),
                                            call.watcher.stop(), call.tracker(),
                                            call.finisher.stop(), call.ioctx.close()])
        self.assertTrue(policy.stopping)


class TestPeerWriterGapCoverage(unittest.TestCase):
    def recovered_policy(self):
        policy = live_policy()
        policy.state.load.return_value = (
            {'42': policy._persistent_instance(writer())}, {KEY: directory(6)}, {KEY: 6})
        with patch.object(peer_writer, 'Finisher'), \
                patch.object(peer_writer, 'WriterInstanceWatcher'):
            policy.init()
        self.addCleanup(policy.shutdown)
        return policy

    def owned_policy(self):
        policy = live_policy()
        policy.update_instances({'42': writer()}, {})
        assign(policy)
        return policy

    def check_shutdown_completion(self, result):
        policy = self.owned_policy()
        before = copy.deepcopy(policy.directories[KEY])
        policy.notifier, policy.finisher = Mock(), Mock()
        policy.finisher.queue.side_effect = lambda callback, args: callback(*args)
        message = policy._message('acquire', KEY, before)
        policy.operation_status[KEY] = {'state': 'acquiring',
                                        'operation_id': message['operation_id']}
        status = copy.deepcopy(policy.operation_status[KEY])
        PeerWriterPolicy._notify(policy, '42', KEY, message)
        self.assertEqual(policy.op_tracker.ops_in_progress, 1)
        waiting, errors = threading.Event(), []
        original_wait = policy.op_tracker.cond.wait

        def wait(*args):
            waiting.set()
            return original_wait(*args)

        def shutdown():
            try:
                policy.shutdown()
            except Exception as error:
                errors.append(error)

        with patch.object(policy.op_tracker.cond, 'wait', side_effect=wait):
            worker = threading.Thread(target=shutdown, daemon=True)
            worker.start()
            try:
                self.assertTrue(waiting.wait(2), 'shutdown did not wait for the operation')
                policy.finisher.stop.assert_not_called()
                policy.ioctx.close.assert_not_called()
                policy.notifier.notify.call_args.args[2](result)
                worker.join(2)
                self.assertFalse(worker.is_alive(), 'shutdown did not finish')
                self.assertEqual(errors, [])
                self.assertEqual(policy.op_tracker.ops_in_progress, 0)
                self.assertEqual(policy.directories[KEY], before)
                self.assertEqual(policy.operation_status[KEY], status)
                policy.finisher.stop.assert_called_once_with()
                policy.ioctx.close.assert_called_once_with()
            finally:
                # Rescue only a failed test; do not leave its shutdown worker blocked.
                if worker.is_alive():
                    with policy.op_tracker.cond:
                        policy.op_tracker.ops_in_progress = 0
                        policy.op_tracker.cond.notify_all()
                    worker.join(2)
                self.assertFalse(worker.is_alive(), 'shutdown worker leaked')

    def source_control(self):
        control = control_plane()
        del control._verify_source
        control.snapshot_mirror = Mock()
        cluster, handle = Mock(), Mock()
        cluster.get_fsid.return_value = 'fsid'
        handle.get_fscid.return_value = 7
        source_map = {'filesystems': [{'mdsmap': {'fs_name': 'srcfs'},
                                      'mirror_info': {'peers': {
                                          PEER: {'remote': {'fs_name': 'dstfs'}}}}}]}
        cluster.mon_command.return_value = (0, json.dumps(source_map), '')
        patches = [
            patch.object(peer_writer, 'connect_to_filesystem', return_value=(cluster, handle)),
            patch.object(peer_writer, 'disconnect_from_filesystem'),
            patch.object(peer_writer, 'open_filesystem', return_value=MagicMock()),
            patch.object(peer_writer.FSSnapshotMirror, 'get_mirror_info',
                         return_value={'cluster_id': 'fsid', 'fs_id': 7}),
        ]
        mocks = []
        for patcher in patches:
            mocks.append(patcher.start())
            self.addCleanup(patcher.stop)
        return control, cluster, handle, mocks

    def test_shutdown_waits_for_successful_notification_completion(self):
        self.check_shutdown_completion(0)

    def test_shutdown_drains_late_error_without_changing_ownership(self):
        self.check_shutdown_completion(-errno.EIO)

    def test_restart_requires_discovery_and_fences_missing_writer(self):
        policy = self.recovered_policy()
        self.assertEqual(policy.live_instances, {})
        self.assertEqual(policy.operation_status[KEY]['state'], 'discovering')
        with patch.object(peer_writer.time, 'time', return_value=policy.recovery_deadline - 1):
            policy._retry()
        self.assertEqual(policy.operation_status[KEY]['state'], 'discovering')
        with patch.object(peer_writer.time, 'time', return_value=policy.recovery_deadline):
            policy._retry()
        self.assertEqual(policy.operation_status[KEY]['state'], 'fencing')
        self.assertEqual(policy.directories[KEY], directory(6))
        policy.state.update_instance.assert_called_once_with('42', None)
        policy._notify.assert_not_called()

    def test_restart_accepts_same_identity_but_fences_changed_identity(self):
        for changes, expected in [({}, 'assigned'), ({'process_incarnation': 'new'}, 'fencing'),
                                  ({'destination_client_identity': 'new'}, 'fencing')]:
            with self.subTest(changes=changes):
                policy = self.recovered_policy()
                policy.update_instances({'42': writer(**changes)}, {})
                self.assertEqual(policy.operation_status[KEY]['state'], expected)
                self.assertEqual(policy.directories[KEY], directory(6))
                policy._notify.assert_not_called()

    def test_failed_release_keeps_owner_and_epoch_until_retry_succeeds(self):
        policy = self.owned_policy()
        policy.remove_directory(*KEY)
        release = policy._notify.call_args.args[2]
        before = copy.deepcopy(policy.directories[KEY])
        notifications = policy._notify.call_count
        policy.update_instances({'42': writer(addr='new-address')}, {})
        self.assertEqual(policy.operation_status[KEY]['operation_id'], release['operation_id'])
        self.assertEqual(policy._notify.call_count, notifications)
        policy._handle_release(KEY, release['operation_id'], -errno.ETIMEDOUT)
        self.assertEqual(policy.operation_status[KEY]['state'], 'releasing')
        self.assertEqual(policy.directories[KEY], before)
        self.assertEqual(policy.directory_balancer.total_counts, {'a': 1})
        policy._retry()
        retry = policy._notify.call_args.args[2]
        self.assertEqual((retry['mode'], retry['assignment_epoch']), ('release', 1))
        self.assertNotEqual(retry['operation_id'], release['operation_id'])
        notifications = policy._notify.call_count
        policy._retry()
        self.assertEqual(policy._notify.call_count, notifications)
        policy._handle_release(KEY, retry['operation_id'], 0)
        self.assertNotIn(KEY, policy.directories)
        self.assertEqual(policy.epochs[KEY], 1)

        policy = self.owned_policy()
        policy.remove_directory(*KEY)
        release = policy._notify.call_args.args[2]
        before = copy.deepcopy(policy.directories[KEY])
        policy._fence_instance = Mock()
        policy.update_instances({}, {'42': writer()})
        policy._fence_instance.assert_called_once_with('42', {KEY: before})
        self.assertEqual(policy.operation_status[KEY]['state'], 'fencing')
        policy._handle_release(KEY, release['operation_id'], 0)
        self.assertEqual(policy.directories[KEY], before)
        self.assertEqual(policy.epochs[KEY], 1)

    def test_release_persistence_failure_blocks_replacement_until_retry(self):
        policy = self.owned_policy()
        policy.update_instances({'43': writer('b')}, {})
        policy.update_instances({'42': writer(filesystems=('otherfs',))}, {})
        release = policy._notify.call_args.args[2]
        before = copy.deepcopy(policy.directories[KEY])
        notifications = policy._notify.call_count
        policy.state.update_directory.side_effect = MirrorException(-errno.EIO, 'write failed')
        policy._handle_release(KEY, release['operation_id'], 0)
        self.assertEqual(policy.operation_status[KEY]['state'], 'unavailable')
        self.assertEqual(policy.directories[KEY], before)
        self.assertEqual(policy.directory_balancer.total_counts, {'a': 1})
        self.assertEqual(policy._notify.call_count, notifications)
        policy.state.update_directory.side_effect = None
        policy._retry()
        retry = policy._notify.call_args.args[2]
        self.assertEqual(retry['mode'], 'release')
        policy._handle_release(KEY, retry['operation_id'], 0)
        replacement = policy._notify.call_args.args[2]
        self.assertEqual((replacement['instance_id'], replacement['assignment_epoch']), ('43', 2))

    def test_cas_conflict_reloads_external_owner_and_epoch_before_acquiring(self):
        with patch.object(writer_state, 'rados', omap_binding()):
            ioctx = OmapIoctx()
            state = writer_state.WriterState(ioctx)
            state.initialize()
            policy = PeerWriterPolicy(Mock(), Mock(), 'dstfs', state)
            policy._notify, policy._schedule_retry_locked = Mock(), Mock()
            policy.update_instances({'42': writer(), '43': writer('b')}, {})
            old = assign(policy)
            other = writer_state.WriterState(ioctx)
            other.initialize()
            winner = dict(directory(7), instance_id='43', process_incarnation='inc-b',
                          destination_client_identity='client-b')
            other.update_directory(*KEY, winner, 7)
            with self.assertRaises(MirrorException) as raised:
                policy.remove_directory(*KEY)
            self.assertEqual(raised.exception.args[0], -errno.EAGAIN)
            self.assertTrue(state.stale)
            policy._retry()
            acquire = policy._notify.call_args.args[2]
            self.assertEqual((acquire['instance_id'], acquire['assignment_epoch']), ('43', 7))
            policy._handle_acquire(KEY, old['operation_id'], -errno.EIO)
            self.assertEqual(policy.operation_status[KEY]['state'], 'acquiring')
            policy._handle_acquire(KEY, acquire['operation_id'], 0)
            self.assertEqual(policy.operation_status[KEY]['state'], 'assigned')
            self.assertEqual(state.load()[1][KEY], winner)

    def test_source_reciprocal_validation_disconnects_on_success_and_error(self):
        control, cluster, handle, mocks = self.source_control()
        _, disconnect, opened, _ = mocks
        control._verify_source('dstfs', PEER, 'client.writer@srcsite', 'srcfs', {})
        disconnect.assert_called_once_with('srcsite', 'srcfs', cluster, handle)
        opened.return_value.__exit__.assert_called_once()
        disconnect.reset_mock()
        source_map = json.loads(cluster.mon_command.return_value[1])
        source_map['filesystems'][0]['mirror_info']['peers'][PEER]['remote']['fs_name'] = 'otherfs'
        cluster.mon_command.return_value = (0, json.dumps(source_map), '')
        with self.assertRaises(MirrorException) as raised:
            control._verify_source('dstfs', PEER, 'client.writer@srcsite', 'srcfs', {})
        self.assertEqual(raised.exception.args[0], -errno.ENOENT)
        disconnect.assert_called_once_with('srcsite', 'srcfs', cluster, handle)

    def test_source_identity_mismatch_and_connect_failure_clean_up_correctly(self):
        control, cluster, handle, mocks = self.source_control()
        connect, disconnect, opened, mirror_info = mocks
        for conf, info in [({'fsid': 'wrong'}, {'cluster_id': 'fsid', 'fs_id': 7}),
                           ({}, {'cluster_id': 'wrong', 'fs_id': 7}),
                           ({}, {'cluster_id': 'fsid', 'fs_id': 8})]:
            with self.subTest(conf=conf, info=info):
                disconnect.reset_mock()
                mirror_info.return_value = info
                with self.assertRaises(MirrorException) as raised:
                    control._verify_source('dstfs', PEER, 'client.writer@srcsite', 'srcfs', conf)
                self.assertEqual(raised.exception.args[0], -errno.EINVAL)
                disconnect.assert_called_once_with('srcsite', 'srcfs', cluster, handle)
        self.assertEqual(opened.return_value.__exit__.call_count, 2)
        disconnect.reset_mock()
        connect.side_effect = MirrorException(-errno.EIO, 'connect failed')
        with self.assertRaises(MirrorException):
            control._verify_source('dstfs', PEER, 'client.writer@srcsite', 'srcfs', {})
        disconnect.assert_not_called()

    def test_policy_opening_reuses_cache_and_closes_failed_ioctx(self):
        for stage in ('stat', 'initialize', 'init', 'success'):
            with self.subTest(stage=stage):
                control = control_plane()
                del control._open_policy
                control.policies, control.rados = {}, Mock()
                control.directory_balancer = PeerWriterPolicy.DirectoryBalancer()
                control.fs_map['filesystems'][0]['mdsmap']['metadata_pool'] = 23
                ioctx = control.rados.open_ioctx2.return_value
                with patch.object(peer_writer, 'WriterState') as state, \
                        patch.object(peer_writer, 'PeerWriterPolicy') as policy:
                    if stage == 'stat':
                        ioctx.stat.side_effect = peer_writer.rados.Error('missing', errno.ENOENT)
                        self.assertIsNone(control._open_policy('dstfs', False))
                    elif stage == 'success':
                        opened = control._open_policy('dstfs', True)
                        self.assertIs(opened, policy.return_value)
                        self.assertIs(control._open_policy('dstfs', True), opened)
                        state.return_value.initialize.assert_called_once_with()
                        opened.init.assert_called_once_with()
                    else:
                        target = (state.return_value.initialize if stage == 'initialize'
                                  else policy.return_value.init)
                        target.side_effect = MirrorException(-errno.EIO, stage)
                        with self.assertRaises(MirrorException):
                            control._open_policy('dstfs', True)
                control.rados.open_ioctx2.assert_called_once_with(23)
                if stage == 'success':
                    ioctx.close.assert_not_called()
                    self.assertEqual(control.policies, {'dstfs': opened})
                else:
                    ioctx.close.assert_called_once_with()
                    self.assertEqual(control.policies, {})
