"""Check that a lookup from another client sees a kernel client's async create.

Usage: async_dirop_visibility.py <arm-dir> [iterations]
(arm started with cluster.py; mds_allow_async_dirops must be true)

Per iteration, in a fresh directory /v/dN that only the kernel client uses:
  1. the nowsync kernel client creates a few primer files synchronously; the
     MDS creates a CREATE lock cache and grants DIR_CREATE to the loner;
  2. the MDS process is paused (SIGSTOP, this arm's MDS only) and the kernel
     client creates /v/dN/a. If open() returns while the MDS is paused, the
     create was asynchronous;
  3. a libcephfs observer mounts and looks up /v/dN/a, the MDS is resumed
     (SIGCONT), and the lookup must find the file. This is what the
     path_traverse filelock wrlock on a directory with a lock cache provides
     ("force client to flush async dir operation").
The MDS is paused for at most a few seconds, well below the beacon grace.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

scratch = Path(__file__).resolve().parent
sys.path.insert(0, str(scratch))
import cluster  # noqa: E402

arm = Path(sys.argv[1]).resolve()
iterations = int(sys.argv[2]) if len(sys.argv) > 2 else 20
env = cluster.arm_env(arm)
if os.environ.get('REPRO_REEXEC') != '1':
    env['REPRO_REEXEC'] = '1'
    os.execve(env['PYTHON'], [env['PYTHON'], '-B', __file__] + sys.argv[1:], env)

import cephfs  # noqa: E402

conf = str(arm / 'ceph.conf')
ceph = str(arm / 'bin/ceph')
ev = arm / 'evidence-async-visibility'
ev.mkdir(exist_ok=False)


def run(args, timeout=60):
    return subprocess.run(args, cwd=arm, env=env, capture_output=True, text=True,
                          timeout=timeout, check=True).stdout


def asok(*cmd):
    path = str(next((arm / 'asok').glob('*mds.*.asok')))
    out = run([ceph, '--admin-daemon', path] + list(cmd), timeout=30)
    try:
        return json.loads(out)
    except ValueError:
        return out


def client(role):
    fs = cephfs.LibCephFS(conffile=conf)
    fs.conf_set('client_metadata', 'repro_role=' + role)
    fs.mount()
    return fs


class Call(threading.Thread):
    def __init__(self, fn):
        super().__init__(daemon=True)
        self.fn, self.error = fn, None

    def run(self):
        try:
            self.fn()
        except Exception as e:  # recorded, not raised
            self.error = type(e).__name__


def mds_signal(sig):
    """Signal this arm's MDS only (identified by the arm's CEPH_CONF)."""
    pids = [pid for pid, exe in cluster.owned_processes(arm) if exe.endswith('ceph-mds')]
    assert len(pids) == 1, pids
    os.kill(pids[0], sig)


def create(path):
    with open(path, 'w'):
        pass


flag = asok('config', 'get', 'mds_allow_async_dirops')['mds_allow_async_dirops']
assert flag == 'true', flag

setup = client('setup')
setup.mkdir('/v', 0o755)
setup.unmount()
setup.shutdown()

mon = json.loads(run([ceph, 'mon', 'dump', '-f', 'json']))
v1 = [a['addr'] for a in mon['mons'][0]['public_addrs']['addrvec'] if a['type'] == 'v1'][0]
key = run([ceph, 'auth', 'get-key', 'client.admin']).strip()
mnt = arm / 'mnt-k'
run(['mount', '-t', 'ceph', v1 + ':/', str(mnt), '-o', 'name=admin,secret=%s,nowsync' % key])
vdir = mnt / 'v'
os.listdir(vdir)

rows, failures = [], []
observers = []


def observe(i):
    # mounts while the MDS is paused, so it holds no caps on /v/dN before the
    # kernel client's create
    fs = client('observer%d' % i)
    observers.append(fs)
    fs.stat('/v/d%d/a' % i)


for i in range(iterations):
    d = vdir / ('d%d' % i)              # fresh directory: the kernel client is its only client
    os.mkdir(d)
    for n in range(3):                  # primers: sync creates; the MDS creates the CREATE
        create(d / ('p%d' % n))         # lock cache and grants DIR_CREATE to the loner
        time.sleep(0.3)
    time.sleep(1)
    k = Call(lambda: create(d / 'a'))
    o = Call(lambda: observe(i))
    mds_signal(signal.SIGSTOP)
    try:
        k.start()
        k.join(2)
        kernel_async = not k.is_alive()
        o.start()
        o.join(0.3)
        observer_returned_while_paused = not o.is_alive()
    finally:
        mds_signal(signal.SIGCONT)
    k.join(30)
    o.join(30)
    row = {'i': i, 'kernel_create_async': kernel_async, 'kernel_error': k.error,
           'observer_result': o.error or 'found',
           'observer_returned_while_paused': observer_returned_while_paused}
    if k.is_alive() or o.is_alive():
        row['stuck'] = True
        failures.append('iteration %d: request stuck' % i)
    elif k.error or o.error:
        failures.append('iteration %d: create %s, lookup %s' % (i, k.error, o.error))
    rows.append(row)
    for fs in observers:
        fs.unmount()
        fs.shutdown()
    observers.clear()

result = {'iterations': iterations, 'failures': failures,
          'async_creates_proven': sum(r['kernel_create_async'] for r in rows), 'rows': rows}
result['ok'] = not failures and result['async_creates_proven'] > 0
subprocess.run(['umount', str(mnt)], timeout=60)
(ev / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
print('RESULT ' + json.dumps({k: v for k, v in result.items() if k != 'rows'}), flush=True)
os._exit(0 if result['ok'] else 1)
