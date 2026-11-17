"""Drive the lookup-retry lock cycle schedule on an isolated vstart cluster.

Usage: repro.py <arm-dir> <mode>

modes (see README.md):
  borrow    kernel client's async unlink borrows its UNLINK lock cache; a
            lookup retry then requests a parent ifile wrlock while holding the
            dentry rdlock -> deadlock after all delay points are released
  prevent   mds_allow_async_dirops=false (verified) before the kernel unlinks
  toggle    borrow, then mds_allow_async_dirops=false after the cycle formed
  direct    a different (libcephfs) client unlinks, kernel cap acks deferred;
            documents the variant that does not form a cycle
  restart   kernel client first builds real lock caches; then
            mds_allow_async_dirops=false is set persistently (ceph config set),
            the MDS is failed/respawned, and the borrow schedule is run
            (REPRO_CONFIG_WHO selects the config section, default mds)

Actors (each a separate CephFS client session):
  H  libcephfs  unlinks /e/h (hard link to F); held after taking F.snap X
  L  libcephfs  lookup /d/f  (takes dentry N RD, waits on F.snap RD)
  K  kernel     nowsync mount; sync unlink creates the UNLINK lock cache,
                its next (async) unlink of /d/f is U
  G  libcephfs  getattr/lookup of /d (starts the D.file transition to sync)
"""
import glob
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

scratch = Path(__file__).resolve().parent
sys.path.insert(0, str(scratch))
import cluster  # noqa: E402

arm = Path(sys.argv[1]).resolve()
mode = sys.argv[2]
assert mode in ('borrow', 'prevent', 'toggle', 'direct', 'restart'), mode
env = cluster.arm_env(arm)
if os.environ.get('REPRO_REEXEC') != '1':
    env['REPRO_REEXEC'] = '1'
    os.execve(env['PYTHON'], [env['PYTHON'], '-B', __file__] + sys.argv[1:], env)

import cephfs  # noqa: E402

conf = str(arm / 'ceph.conf')
ceph = str(arm / 'bin/ceph')
ev = arm / ('evidence-' + mode)
ev.mkdir(exist_ok=False)
log_lines = []


def log(msg):
    line = '%.6f %s' % (time.time(), msg)
    print(line, flush=True)
    log_lines.append(line)
    (ev / 'driver.log').write_text('\n'.join(log_lines) + '\n')


def run(args, timeout=60, check=True):
    p = subprocess.run(args, cwd=arm, env=env, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError('%s failed %d: %s' % (args, p.returncode, p.stderr))
    return p.stdout


def asok(*cmd, save=None):
    path = glob.glob(str(arm / 'asok/*mds.*.asok'))
    assert len(path) == 1, path
    out = run([ceph, '--admin-daemon', path[0]] + list(cmd), timeout=30)
    if save:
        (ev / (save + '.json')).write_text(out)
    try:
        return json.loads(out)
    except ValueError:
        return out


def client(role):
    fs = cephfs.LibCephFS(conffile=conf)
    fs.conf_set('client_metadata', 'repro_role=' + role)
    fs.mount()
    return fs


def session_id(role):
    for s in asok('session', 'ls'):
        if s.get('client_metadata', {}).get('repro_role') == role:
            return s['id']
    raise RuntimeError('no session for ' + role)


def kernel_session_id():
    ids = [s['id'] for s in asok('session', 'ls')
           if 'kernel_version' in s.get('client_metadata', {})]
    assert len(ids) == 1, ids
    return ids[0]


def ops():
    return asok('dump_ops_in_flight')['ops']


def op_of(cid, kind):
    kinds = kind if isinstance(kind, tuple) else (kind,)
    for o in ops():
        desc = o['description']
        if desc.startswith('client_request(client.%d:' % cid) and \
                any((' %s ' % k) in desc for k in kinds):
            return o
    return None


def last_event(o):
    return o['type_data']['events'][-1]['event']


def wait_for(what, pred, timeout=30, threads=()):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for t in threads:
            if t.error:
                raise RuntimeError('%s failed: %s' % (t.name, t.error))
        r = pred()
        if r:
            log('ok: ' + what + ' -> ' + (r if isinstance(r, str) else 'true'))
            return r
        time.sleep(0.2)
    asok('dump_ops_in_flight', save='timeout-ops')
    raise RuntimeError('timed out waiting for ' + what)


def waiting(cid, kind, needle):
    def pred():
        o = op_of(cid, kind)
        if o and needle in last_event(o):
            return '%s %s' % (o['description'], last_event(o))
        return None
    return pred


class Blocking(threading.Thread):
    def __init__(self, name, fn):
        super().__init__(name=name, daemon=True)
        self.fn, self.result, self.error, self.done_at = fn, None, None, None

    def run(self):
        try:
            self.result = self.fn()
        except Exception as e:  # recorded, not raised
            self.error = repr(e)
        self.done_at = time.time()


def set_async_dirops(value):
    """Change the option in the running MDS (admin socket = override level,
    above ceph.conf) and verify the daemon really uses the new value."""
    v = 'true' if value else 'false'
    asok('config', 'set', 'mds_allow_async_dirops', v)
    got = asok('config', 'get', 'mds_allow_async_dirops')['mds_allow_async_dirops']
    if got != v:
        raise RuntimeError('mds_allow_async_dirops is %r, expected %r' % (got, v))
    log('mds_allow_async_dirops=%s (verified in the MDS)' % v)


def snapshot(tag):
    asok('dump_ops_in_flight', save=tag + '-ops')
    asok('unlink_hold_cap_deferral', 'status', save=tag + '-fault')
    for name, ino in (('D', D_ino), ('F', F_ino)):
        asok('dump', 'inode', str(ino), save='%s-inode-%s' % (tag, name))


# ---------------------------------------------------------------- setup
log('mode ' + mode)
setup = client('setup')
setup.mkdir('/d', 0o755)
setup.mkdir('/e', 0o755)
fd = setup.open('/d/f', 'w', 0o644)
setup.close(fd)
setup.link('/d/f', '/e/h')
fd = setup.open('/d/x1', 'w', 0o644)   # unlinked by the kernel client in step 3
setup.close(fd)
fd = setup.open('/d/x0', 'w', 0o644)   # restart mode: builds a lock cache first
setup.close(fd)
D_ino = setup.stat('/d').st_ino
F_ino = setup.stat('/d/f').st_ino
setup.unmount()
setup.shutdown()
log('setup: D=%#x F=%#x' % (D_ino, F_ino))

mon = json.loads(run([ceph, 'mon', 'dump', '-f', 'json']))
v1 = [a['addr'] for a in mon['mons'][0]['public_addrs']['addrvec'] if a['type'] == 'v1'][0]
key = run([ceph, 'auth', 'get-key', 'client.admin']).strip()
mnt = arm / 'mnt-k'
run(['mount', '-t', 'ceph', v1 + ':/', str(mnt), '-o',
     'name=admin,secret=%s,nowsync' % key], timeout=60)
log('kernel mount nowsync at %s via %s' % (mnt, v1))
os.listdir(mnt / 'd')          # kernel client gets caps on D
os.stat(mnt / 'd' / 'f')       # kernel client knows dentry f (needed for async unlink)
os.stat(mnt / 'd' / 'x1')
kc = kernel_session_id()
log('kernel client id %d' % kc)



def dir_op_caps(client_id):
    """DIR_CREATE (Fc) / DIR_UNLINK (Fr) bits issued to a client on D."""
    caps = {c['client_id']: c['issued'] for c in asok('dump', 'inode', str(D_ino))['client_caps']}
    issued = caps.get(client_id, '')
    fbits = issued.split('F', 1)[1] if 'F' in issued else ''
    return issued, [b for b in 'cr' if b in fbits]


def mds_log_count(needle, after_respawn=False):
    """Count MDS log lines containing needle.  With after_respawn, only lines
    written by the respawned process count: everything after the last
    'respawning with exe' marker, excluding the '-NNN>' recent-event dump."""
    import re
    with open(arm / 'out/mds.a.log', errors='replace') as f:
        lines = f.readlines()
    if after_respawn:
        marks = [i for i, line in enumerate(lines) if 'respawning with exe' in line]
        lines = lines[marks[-1] + 1:] if marks else []
    dump = re.compile(r'^\s*-?\d+>')
    return sum(1 for line in lines if needle in line and not dump.match(line))


restart_info = None
if mode == 'restart':
    # a. with async dirops on, the kernel client builds real lock caches
    with open(mnt / 'd' / 'w0', 'w') as f:      # create -> CREATE lock cache
        f.write('x')
    os.unlink(mnt / 'd' / 'x0')                 # unlink -> UNLINK lock cache
    time.sleep(1)
    before = dir_op_caps(kc)
    created_before = mds_log_count('create_lock_cache for client.%d/' % kc)
    log('before restart: kernel caps on D %s dir-op bits %s, lock caches created %d'
        % (before[0], before[1], created_before))
    if not created_before or not before[1]:
        raise RuntimeError('precondition failed: no kernel lock cache / dir-op caps')
    snapshot('r0-before-restart')
    # b. persistent setting via the monitors, then fail the MDS (it respawns,
    #    replays the journal and clients reconnect)
    who = os.environ.get('REPRO_CONFIG_WHO', 'mds')   # e.g. mds or global
    run([ceph, 'config', 'set', who, 'mds_allow_async_dirops', 'false'])
    log('ceph config set %s mds_allow_async_dirops false' % who)
    old_pid = [p for p, exe in cluster.owned_processes(arm) if exe.endswith('ceph-mds')]
    run([ceph, 'mds', 'fail', 'a'])
    log('ceph mds fail a (pid %s)' % old_pid)

    def mds_active():
        try:
            st = json.loads(run([ceph, 'fs', 'status', '-f', 'json'], check=False) or '{}')
            ranks = [m for m in st.get('mdsmap', []) if m.get('rank') == 0]
            return bool(ranks) and ranks[0].get('state') == 'active' and \
                asok('status')['state'] == 'up:active'
        except Exception:
            return False
    wait_for('MDS active again after fail', mds_active, timeout=180)
    wait_for('kernel client reconnected',
             lambda: any(s['id'] == kc and s['state'] == 'open'
                         for s in asok('session', 'ls')), timeout=120)
    os.listdir(mnt / 'd')
    os.stat(mnt / 'd' / 'f')
    os.stat(mnt / 'd' / 'x1')
    time.sleep(1)
    got = asok('config', 'get', 'mds_allow_async_dirops')['mds_allow_async_dirops']
    after = dir_op_caps(kc)
    log('after restart: option %s, kernel caps on D %s dir-op bits %s' % (got, after[0], after[1]))
    if got != 'false':
        raise RuntimeError('option not false after restart: %r' % got)
    restart_info = {'config_who': who,
                    'kernel_caps_before': before[0], 'dir_op_bits_before': before[1],
                    'lock_caches_created_before': created_before,
                    'option_after_restart': got, 'kernel_caps_after': after[0],
                    'dir_op_bits_after': after[1]}
    snapshot('r1-after-restart')

H, L, U, G = (client(r) for r in ('H', 'L', 'U', 'G'))
hid, lid, uid, gid = (session_id(r) for r in ('H', 'L', 'U', 'G'))
log('client ids H=%d L=%d U=%d G=%d K=%d' % (hid, lid, uid, gid, kc))
asok('unlink_hold_cap_deferral', 'clear_events')
log('mds_allow_async_dirops at start: %s' % asok('config', 'get', 'mds_allow_async_dirops')['mds_allow_async_dirops'])
snapshot('00-initial')

# ---------------------------------------------------------------- schedule
borrow = mode in ('borrow', 'prevent', 'toggle', 'restart')
if mode == 'direct':
    U.stat('/d/f')             # libcephfs U unlinks without a fresh lookup

# 1. H's unlink of /e/h acquires F.snap X and is held there (snapshot-lock
#    holder; the only artificial delay in the borrow modes).
asok('unlink_hold_cap_deferral', 'arm_unlink_hold', 'h')
th = Blocking('H-unlink', lambda: H.unlink('/e/h'))
th.start()
wait_for('H unlink holds F.snap X',
         lambda: asok('unlink_hold_cap_deferral', 'status')['unlink_held'])

# 2. L looks up /d/f: takes dentry N RD, then waits on F.snap RD.
tl = Blocking('L-lookup', lambda: L.stat('/d/f'))
tl.start()
wait_for('L waits on F.snap', waiting(lid, 'lookup', 'failed to rdlock'), threads=[tl])
snapshot('01-L-waiting')

if mode == 'prevent':
    set_async_dirops(False)    # before the kernel client unlinks

if borrow:
    # 3. kernel client's first unlink in D is synchronous; the MDS creates an
    #    UNLINK lock cache K on D and issues DIR_UNLINK to the kernel client.
    os.unlink(mnt / 'd' / 'x1')
    log('kernel unlinked /d/x1')
    time.sleep(1)
    snapshot('02-after-kernel-unlink')
    # 4. kernel client's next unlink is asynchronous (nowsync); the MDS
    #    attaches it to K (borrowing K's D.file/D.nest WR) and it waits on N X.
    tu = Blocking('U-unlink', lambda: os.unlink(mnt / 'd' / 'f'))
    tu.start()
    u_owner = kc
else:
    # 3. kernel create -> CREATE lock cache; 4. libcephfs U unlinks directly.
    with open(mnt / 'd' / 'k1', 'w') as f:
        f.write('x')
    log('kernel created /d/k1')
    time.sleep(1)
    snapshot('02-after-kernel-create')
    tu = Blocking('U-unlink', lambda: U.unlink('/d/f'))
    tu.start()
    u_owner = uid
wait_for('U waits on dentry xlock', waiting(u_owner, 'unlink', 'failed to xlock'), threads=[tu])
snapshot('025-U-waiting')
if mode == 'direct':
    asok('unlink_hold_cap_deferral', 'arm_defer_caps', str(kc))

# 5. G getattr on D needs D.file RD: starts LOCK/EXCL -> SYNC, invalidating
#    lock caches on D.
tg = Blocking('G-getattr', lambda: G.statx('/d', cephfs.CEPH_STATX_SIZE |
                                           cephfs.CEPH_STATX_MTIME, 0))
tg.start()
wait_for('G getattr waits on D.file', waiting(gid, ('getattr', 'lookup'), 'failed to rdlock'),
         threads=[tg])
snapshot('03-G-waiting')

# 6. release H: F.snap X drops and L retries its traversal.
asok('unlink_hold_cap_deferral', 'release_unlink_hold')
asok('unlink_hold_cap_deferral', 'release_defer_caps')
log('all delay points released')
time.sleep(2)
snapshot('04-after-release')


def mds_pending():
    """Requests of the actors still in the MDS (kernel U may return early)."""
    left = {}
    for name, cid, kind in (('L', lid, 'lookup'), ('U', u_owner, 'unlink'),
                            ('G', gid, ('getattr', 'lookup'))):
        o = op_of(cid, kind)
        if o:
            left[name] = {'description': o['description'],
                          'last_event': last_event(o), 'age': o['age']}
    return left


def observe(seconds):
    deadline = time.time() + seconds
    while time.time() < deadline and mds_pending():
        time.sleep(1)
    return mds_pending()


# ---------------------------------------------------------------- verdict
OBSERVE = 60
result = {'mode': mode, 'observe_seconds_after_release': OBSERVE,
          'ids': {'H': hid, 'L': lid, 'U': u_owner, 'G': gid, 'K': kc},
          'D_ino': D_ino, 'F_ino': F_ino}
pending = observe(OBSERVE)
snapshot('05-after-observe')
result['pending_after_release'] = pending
result['deadlock'] = bool(pending)
if mode == 'toggle':
    result['deadlock_before_toggle'] = bool(pending)
    set_async_dirops(False)    # at runtime, cycle already formed
    pending = observe(OBSERVE)
    snapshot('06-after-toggle')
    result['pending_after_toggle'] = pending
    result['released_by_toggle'] = result['deadlock_before_toggle'] and not pending
if restart_info is not None:
    restart_info['lock_caches_created_after_restart'] = mds_log_count(
        'create_lock_cache for client.', after_respawn=True)
    restart_info['lock_cache_attach_after_restart'] = mds_log_count(
        'found lock cache for', after_respawn=True)
    result['restart'] = restart_info
for t in (th, tl, tu, tg):
    result[t.name] = {'returned': bool(t.done_at), 'error': t.error}
(ev / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
log('RESULT ' + json.dumps(result))
os._exit(0)   # blocked client threads cannot be joined; cluster.py stop cleans up
