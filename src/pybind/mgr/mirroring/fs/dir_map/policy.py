import os
import errno
import logging
import time
from threading import Lock
from typing import Dict

from .state_transition import ActionType, PolicyAction, Transition, \
    State, StateTransition
from ..exception import MirrorException

log = logging.getLogger(__name__)

class GlobalDirectoryBalancer:
    def __init__(self):
        self.lock = Lock()
        self.dir_owners = {}  # type: Dict[str, str]
        self.daemon_dir_counts = {}  # type: Dict[str, int]

    @staticmethod
    def dir_key(fs_name, dir_path):
        return f'{fs_name}:{dir_path}'

    def _inc(self, daemon_id):
        self.daemon_dir_counts[daemon_id] = self.daemon_dir_counts.get(daemon_id, 0) + 1

    def _dec(self, daemon_id):
        if not daemon_id:
            return
        count = self.daemon_dir_counts.get(daemon_id, 0)
        if count <= 1:
            self.daemon_dir_counts.pop(daemon_id, None)
        else:
            self.daemon_dir_counts[daemon_id] = count - 1

    def register_mapping(self, fs_name, dir_path, daemon_id):
        if not daemon_id:
            return
        with self.lock:
            self.register_mapping_locked(fs_name, dir_path, daemon_id)

    def register_mapping_locked(self, fs_name, dir_path, daemon_id):
        key = GlobalDirectoryBalancer.dir_key(fs_name, dir_path)
        old_daemon_id = self.dir_owners.get(key)
        if old_daemon_id == daemon_id:
            return
        self._dec(old_daemon_id)
        self.dir_owners[key] = daemon_id
        self._inc(daemon_id)

    def unregister_mapping(self, fs_name, dir_path):
        with self.lock:
            self.unregister_mapping_locked(fs_name, dir_path)

    def unregister_mapping_locked(self, fs_name, dir_path):
        key = GlobalDirectoryBalancer.dir_key(fs_name, dir_path)
        old_daemon_id = self.dir_owners.pop(key, None)
        self._dec(old_daemon_id)

    def choose_instance(self, instance_to_daemon, instance_to_dir_count):
        with self.lock:
            return self.choose_instance_locked(instance_to_daemon, instance_to_dir_count)

    def choose_instance_locked(self, instance_to_daemon, instance_to_dir_count):
        min_instance_id = None
        min_global_count = None
        min_local_count = None
        for instance_id in sorted(instance_to_daemon.keys()):
            daemon_id = instance_to_daemon[instance_id]
            global_count = self.daemon_dir_counts.get(daemon_id, 0)
            local_count = instance_to_dir_count.get(instance_id, 0)
            if min_instance_id is None or \
               global_count < min_global_count or \
               (global_count == min_global_count and local_count < min_local_count):
                min_instance_id = instance_id
                min_global_count = global_count
                min_local_count = local_count
        return min_instance_id

    def remove_filesystem(self, fs_name):
        with self.lock:
            prefix = f'{fs_name}:'
            for key in list(self.dir_owners.keys()):
                if key.startswith(prefix):
                    old_daemon_id = self.dir_owners.pop(key)
                    self._dec(old_daemon_id)

class DirectoryState:
    def __init__(self, instance_id=None, mapped_time=None):
        self.instance_id = instance_id
        self.mapped_time = mapped_time
        self.state = State.UNASSOCIATED
        self.stalled = False
        self.transition = Transition(ActionType.NONE)
        self.next_state = None
        self.purging = False

    def __str__(self):
        return f'[instance_id={self.instance_id}, mapped_time={self.mapped_time},'\
            f' state={self.state}, transition={self.transition}, next_state={self.next_state},'\
            f' purging={self.purging}]'

class Policy:
    # number of seconds after which a directory can be reshuffled
    # to other mirror daemon instances.
    DIR_SHUFFLE_THROTTLE_INTERVAL = 300

    def __init__(self, fs_name=None, global_dir_balancer=None):
        self.dir_states = {}
        self.instance_to_dir_map = {}
        self.instance_to_daemon = {}
        self.dead_instances = []
        self.lock = Lock()
        self.fs_name = fs_name
        self.global_dir_balancer = global_dir_balancer


    @staticmethod
    def is_instance_action(action_type):
        return action_type in (ActionType.ACQUIRE,
                               ActionType.RELEASE)

    def is_dead_instance(self, instance_id):
        return instance_id in self.dead_instances

    def is_state_scheduled(self, dir_state, state):
        return dir_state.state == state or dir_state.next_state == state

    def is_shuffling(self, dir_path):
        log.debug(f'is_shuffling: {dir_path}')
        return self.is_state_scheduled(self.dir_states[dir_path], State.SHUFFLING)

    def can_shuffle_dir(self, dir_path):
        """Right now, shuffle directories only based on idleness. Later, we
        probably want to avoid shuffling images that were recently shuffled.
        """
        log.debug(f'can_shuffle_dir: {dir_path}')
        dir_state = self.dir_states[dir_path]
        return StateTransition.is_idle(dir_state.state) and \
            (time.time() - dir_state['mapped_time']) > Policy.DIR_SHUFFLE_THROTTLE_INTERVAL

    def set_state(self, dir_state, state, ignore_current_state=False):
        if not ignore_current_state and dir_state.state == state:
            return False
        elif StateTransition.is_idle(dir_state.state):
            dir_state.state = state
            dir_state.next_state = None
            dir_state.transition = StateTransition.transit(
                dir_state.state, dir_state.transition.action_type)
            return True
        dir_state.next_state = state
        return False

    def init(self, dir_mapping):
        with self.lock:
            for dir_path, dir_map in dir_mapping.items():
                instance_id = dir_map['instance_id']
                if instance_id:
                    if not instance_id in self.instance_to_dir_map:
                        self.instance_to_dir_map[instance_id] = []
                    self.instance_to_dir_map[instance_id].append(dir_path)
                self.dir_states[dir_path] = DirectoryState(instance_id, dir_map['last_shuffled'])
                dir_state = self.dir_states[dir_path]
                if self.global_dir_balancer:
                    daemon_id = self.instance_to_daemon.get(instance_id, instance_id)
                    self.global_dir_balancer.register_mapping(self.fs_name, dir_path, daemon_id)
                state = State.INITIALIZING if instance_id else State.ASSOCIATING
                purging = dir_map.get('purging', 0)
                if purging:
                    dir_state.purging = True
                    state = State.DISASSOCIATING
                    if not instance_id:
                        dir_state.transition = StateTransition.transit(state,
                                                                       dir_state.transition.action_type)
                log.debug(f'starting state: {dir_path} {state}: {dir_state}')
                self.set_state(dir_state, state)
                log.debug(f'init dir_state: {dir_state}')

    def lookup(self, dir_path):
        log.debug(f'looking up {dir_path}')
        with self.lock:
            dir_state = self.dir_states.get(dir_path, None)
            if dir_state:
                return {'instance_id': dir_state.instance_id,
                        'mapped_time': dir_state.mapped_time,
                        'purging': dir_state.purging}
            return None

    def map(self, dir_path, dir_state):
        log.debug(f'mapping {dir_path}')
        min_instance_id = None
        assert self.global_dir_balancer is not None
        with self.global_dir_balancer.lock:
            current_instance_id = dir_state.instance_id
            if current_instance_id and not self.is_dead_instance(current_instance_id):
                return True
            if self.is_dead_instance(current_instance_id):
                self.unmap_locked(dir_path, dir_state)
            instance_to_daemon = {}
            instance_to_dir_count = {}
            for instance_id in self.instance_to_dir_map.keys():
                if self.is_dead_instance(instance_id):
                    continue
                instance_to_daemon[instance_id] = self.instance_to_daemon.get(instance_id, instance_id)
                instance_to_dir_count[instance_id] = len(self.instance_to_dir_map[instance_id])
            min_instance_id = self.global_dir_balancer.choose_instance_locked(instance_to_daemon,
                                                                               instance_to_dir_count)
            if not min_instance_id:
                log.debug(f'instance unavailable for {dir_path}')
                return False
            log.debug(f'dir_path {dir_path} maps to instance {min_instance_id}')
            dir_state.instance_id = min_instance_id
            dir_state.mapped_time = time.time()
            self.instance_to_dir_map[min_instance_id].append(dir_path)
            daemon_id = self.instance_to_daemon.get(min_instance_id, min_instance_id)
            self.global_dir_balancer.register_mapping_locked(self.fs_name, dir_path, daemon_id)
            return True

    def unmap_locked(self, dir_path, dir_state):
        instance_id = dir_state.instance_id
        log.debug(f'unmapping {dir_path} from instance {instance_id}')
        self.instance_to_dir_map[instance_id].remove(dir_path)
        if self.global_dir_balancer:
            self.global_dir_balancer.unregister_mapping_locked(self.fs_name, dir_path)
        dir_state.instance_id = None
        dir_state.mapped_time = None
        if self.is_dead_instance(instance_id) and not self.instance_to_dir_map[instance_id]:
            self.instance_to_dir_map.pop(instance_id)
            self.instance_to_daemon.pop(instance_id, None)
            self.dead_instances.remove(instance_id)

    def unmap(self, dir_path, dir_state):
        assert self.global_dir_balancer is not None
        with self.global_dir_balancer.lock:
            self.unmap_locked(dir_path, dir_state)

    def shuffle(self, dirs_per_instance, include_stalled_dirs):
        log.debug(f'directories per instance: {dirs_per_instance}')
        shuffle_dirs = []
        for instance_id, dir_paths in self.instance_to_dir_map.items():
            cut_off = len(dir_paths) - dirs_per_instance
            if cut_off > 0:
                for dir_path in dir_paths:
                    if cut_off == 0:
                        break
                    if self.is_shuffling(dir_path):
                        cut_off -= 1
                    elif self.can_shuffle_dir(dir_path):
                        cut_off -= 1
                        shuffle_dirs.append(dir_path)
        if include_stalled_dirs:
            for dir_path, dir_state in self.dir_states.items():
                if dir_state.stalled:
                    log.debug(f'{dir_path} is stalled: {dir_state} -- trigerring kick')
                    dir_state.stalled = False
                    shuffle_dirs.append(dir_path)
        return shuffle_dirs

    def execute_policy_action(self, dir_path, dir_state, policy_action):
        log.debug(f'executing for directory {dir_path} policy_action {policy_action}')

        done = True
        if policy_action == PolicyAction.MAP:
            done = self.map(dir_path, dir_state)
        elif policy_action == PolicyAction.UNMAP:
            self.unmap(dir_path, dir_state)
        elif policy_action == PolicyAction.REMOVE:
            if dir_state.state == State.UNASSOCIATED:
                self.dir_states.pop(dir_path)
        else:
            raise Exception()
        return done

    def start_action(self, dir_path):
        log.debug(f'start action: {dir_path}')
        with self.lock:
            dir_state = self.dir_states.get(dir_path, None)
            if not dir_state:
                raise Exception()
            log.debug(f'dir_state: {dir_state}')
            if dir_state.transition.start_policy_action:
                stalled = not self.execute_policy_action(dir_path, dir_state,
                                                         dir_state.transition.start_policy_action)
                if stalled:
                    next_action = ActionType.NONE
                    if dir_state.purging:
                        dir_state.next_state = None
                        dir_state.state = State.UNASSOCIATED
                        dir_state.transition = StateTransition.transit(State.DISASSOCIATING, ActionType.NONE)
                        self.set_state(dir_state, State.DISASSOCIATING)
                        next_action = dir_state.transition.action_type
                    else:
                        dir_state.stalled = True
                        log.debug(f'state machine stalled')
                    return next_action
            return dir_state.transition.action_type

    def finish_action(self, dir_path, r):
        log.debug(f'finish action {dir_path} r={r}')
        with self.lock:
            dir_state = self.dir_states.get(dir_path, None)
            if not dir_state:
                raise Exception()
            if r < 0 and (not Policy.is_instance_action(dir_state.transition.action_type) or
                          not dir_state.instance_id or
                          not dir_state.instance_id in self.dead_instances):
                return True
            log.debug(f'dir_state: {dir_state}')
            finish_policy_action = dir_state.transition.finish_policy_action
            dir_state.transition = StateTransition.transit(
                dir_state.state, dir_state.transition.action_type)
            log.debug(f'transitioned to dir_state: {dir_state}')
            if dir_state.transition.final_state:
                log.debug('reached final state')
                dir_state.state = dir_state.transition.final_state
                dir_state.transition = Transition(ActionType.NONE)
                log.debug(f'final dir_state: {dir_state}')
            if StateTransition.is_idle(dir_state.state) and dir_state.next_state:
                self.set_state(dir_state, dir_state.next_state)
            pending = not dir_state.transition.action_type == ActionType.NONE
            if finish_policy_action:
                self.execute_policy_action(dir_path, dir_state, finish_policy_action)
            return pending

    def find_tracked_ancestor_or_subtree(self, dir_path):
        for tracked_path, _ in self.dir_states.items():
            comp = [dir_path, tracked_path]
            cpath = os.path.commonpath(comp)
            if cpath in comp:
                what = 'subtree' if cpath == tracked_path else 'ancestor'
                return (tracked_path, what)
        return None

    def add_dir(self, dir_path):
        log.debug(f'adding dir_path {dir_path}')
        with self.lock:
            if dir_path in self.dir_states:
                return False
            as_info = self.find_tracked_ancestor_or_subtree(dir_path)
            if as_info:
                raise MirrorException(-errno.EINVAL, f'{dir_path} is a {as_info[1]} of tracked path {as_info[0]}')
            self.dir_states[dir_path] = DirectoryState()
            dir_state = self.dir_states[dir_path]
            log.debug(f'add dir_state: {dir_state}')
            if dir_state.state == State.INITIALIZING:
                return False
            return self.set_state(dir_state, State.ASSOCIATING)

    def remove_dir(self, dir_path):
        log.debug(f'removing dir_path {dir_path}')
        with self.lock:
            dir_state = self.dir_states.get(dir_path, None)
            if not dir_state:
                return False
            log.debug(f'removing dir_state: {dir_state}')
            dir_state.purging = True
            # advance the state machine with DISASSOCIATING state for removal
            if dir_state.stalled:
                dir_state.state = State.UNASSOCIATED
                dir_state.transition = StateTransition.transit(State.DISASSOCIATING, ActionType.NONE)
            r = self.set_state(dir_state, State.DISASSOCIATING)
            log.debug(f'dir_state: {dir_state}')
            return r

    def add_instances_initial(self, instances):
        """Take care of figuring out instances which no longer exist
        and remove them. This is to be done only once on startup to
        identify instances which were previously removed but directories
        are still mapped (on-disk) to them.
        """
        for instance_id, daemon_id in instances.items():
            if not instance_id in self.instance_to_dir_map:
                self.instance_to_dir_map[instance_id] = []
            self._update_instance_daemon(instance_id, daemon_id)
        dead_instances = []
        for instance_id, _ in self.instance_to_dir_map.items():
            if not instance_id in instances:
                dead_instances.append(instance_id)
        if dead_instances:
            self._remove_instances(dead_instances)

    def _update_instance_daemon(self, instance_id, daemon_id):
        old_daemon_id = self.instance_to_daemon.get(instance_id)
        if old_daemon_id == daemon_id:
            return
        self.instance_to_daemon[instance_id] = daemon_id
        if not old_daemon_id:
            return
        if self.global_dir_balancer:
            for dir_path in self.instance_to_dir_map.get(instance_id, []):
                self.global_dir_balancer.unregister_mapping(self.fs_name, dir_path)
                self.global_dir_balancer.register_mapping(self.fs_name, dir_path, daemon_id)

    def add_instances(self, instances, initial_update=False):
        log.debug(f'adding instances: {instances} initial_update {initial_update}')
        with self.lock:
            if initial_update:
                self.add_instances_initial(instances)
            else:
                nr_instances = len(self.instance_to_dir_map)
                nr_dead_instances = len(self.dead_instances)
                if nr_instances > 0:
                    # adjust dead instances
                    nr_instances -= nr_dead_instances
                include_stalled_dirs = nr_instances == 0
                for instance_id, daemon_id in instances.items():
                    if not instance_id in self.instance_to_dir_map:
                        self.instance_to_dir_map[instance_id] = []
                    self._update_instance_daemon(instance_id, daemon_id)
                dirs_per_instance = int(len(self.dir_states) /
                                        (len(self.instance_to_dir_map) - nr_dead_instances))
                if dirs_per_instance == 0:
                    dirs_per_instance += 1
                shuffle_dirs = []
                # super set of directories which are candidates for shuffling -- choose
                # those which can be shuffle rightaway (others will be shuffled when
                # they reach idle state).
                shuffle_dirs_ss = self.shuffle(dirs_per_instance, include_stalled_dirs)
                if include_stalled_dirs:
                    return shuffle_dirs_ss
                for dir_path in shuffle_dirs_ss:
                    dir_state = self.dir_states[dir_path]
                    if self.set_state(dir_state, State.SHUFFLING):
                        shuffle_dirs.append(dir_path)
                log.debug(f'remapping directories: {shuffle_dirs}')
                return shuffle_dirs

    def remove_instances(self, instance_ids):
        with self.lock:
            return self._remove_instances(instance_ids)

    def _remove_instances(self, instance_ids):
        log.debug(f'removing instances: {instance_ids}')
        shuffle_dirs = []
        for instance_id in instance_ids:
            if not instance_id in self.instance_to_dir_map:
                continue
            if not self.instance_to_dir_map[instance_id]:
                self.instance_to_dir_map.pop(instance_id)
                self.instance_to_daemon.pop(instance_id, None)
                continue
            self.dead_instances.append(instance_id)
            dir_paths = self.instance_to_dir_map[instance_id]
            log.debug(f'force shuffling instance_id {instance_id}, directories {dir_paths}')
            for dir_path in dir_paths:
                dir_state = self.dir_states[dir_path]
                if self.is_state_scheduled(dir_state, State.DISASSOCIATING):
                    log.debug(f'dir_path {dir_path} is disassociating, ignoring...')
                    continue
                log.debug(f'shuffling dir_path {dir_path}')
                if self.set_state(dir_state, State.SHUFFLING, True):
                    shuffle_dirs.append(dir_path)
        log.debug(f'shuffling {shuffle_dirs}')
        return shuffle_dirs

    def dir_status(self, dir_path):
        with self.lock:
            dir_state = self.dir_states.get(dir_path, None)
            if not dir_state:
                raise MirrorException(-errno.ENOENT, f'{dir_path} is not tracked')
            res = {} # type: Dict
            if dir_state.stalled:
                res['state'] = 'stalled'
                res['reason'] = 'no mirror daemons running'
            elif dir_state.state == State.ASSOCIATING:
                res['state'] = 'mapping'
            else:
                state = None
                dstate = dir_state.state
                if dstate == State.ASSOCIATING:
                    state = 'mapping'
                elif dstate == State.DISASSOCIATING:
                    state = 'unmapping'
                elif dstate == State.SHUFFLING:
                    state = 'shuffling'
                elif dstate == State.ASSOCIATED:
                    state = 'mapped'
                elif dstate == State.INITIALIZING:
                    state = 'resolving'
                res['state'] = state
                res['instance_id'] = dir_state.instance_id
                res['last_shuffled'] = dir_state.mapped_time
            return res

    def instance_summary(self):
        with self.lock:
            res = {
                'mapping': {}
            } # type: Dict
            for instance_id, dir_paths in self.instance_to_dir_map.items():
                daemon_id = self.instance_to_daemon.get(instance_id, instance_id)
                res['mapping'][instance_id] = {
                    'daemon_id': daemon_id,
                    'directory_count': len(dir_paths)
                }
            return res
