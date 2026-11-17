"""Isolated single-OSD vstart cluster for the lookup-retry lock cycle reproduction.

Usage: cluster.py prepare|start|stop|env <arm-dir>

* Binaries come from CEPH_BUILD (default <repo>/build); CEPH_MDS_BINARY can
  point at a different ceph-mds built from this branch.
* Nothing is installed system-wide; no user-wide stop scripts are used.
  `stop` signals only processes whose environment carries this arm's CEPH_CONF
  and unmounts only mounts below the arm directory.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

scratch = Path(__file__).resolve().parent
# Source tree and build of this branch (override with CEPH_ROOT / CEPH_BUILD).
base = Path(os.environ.get('CEPH_ROOT', scratch.parents[3]))
build = Path(os.environ.get('CEPH_BUILD', base / 'build'))
# Optional replacement MDS binary (e.g. an out-of-tree build of this branch).
fault_mds = Path(os.environ['CEPH_MDS_BINARY']) if os.environ.get('CEPH_MDS_BINARY') else None
python = os.environ.get('REPRO_PYTHON', sys.executable)
DAEMONS = ('ceph-mon', 'ceph-osd', 'ceph-mds', 'ceph-mgr', 'ceph-fuse')


def arm_env(arm: Path, port: str = '44100'):
    config = str(arm / 'ceph.conf')
    env = dict(os.environ)
    env.update({
        'TMPDIR': str(arm / 'tmp'), 'TMP': str(arm / 'tmp'), 'TEMP': str(arm / 'tmp'),
        'PYTHONDONTWRITEBYTECODE': '1', 'CEPH_CONF': config,
        'CEPH_BIN': str(arm / 'bin'), 'CEPH_LIB': str(arm / 'lib'),
        'CEPH_DIR': str(arm), 'CEPH_CONF_PATH': str(arm),
        'CEPH_DEV_DIR': str(arm / 'dev'), 'CEPH_OUT_DIR': str(arm / 'out'),
        'CEPH_OUT_CLIENT_DIR': str(arm / 'out'),
        'CEPH_ASOK_DIR': str(arm / 'asok'),
        'CEPH_PORT': port,
        'MON': '1', 'OSD': '1', 'MDS': '1', 'MGR': '1', 'FS': '1', 'RGW': '0', 'NFS': '0',
        'CEPH_NUM_MON': '1', 'CEPH_NUM_OSD': '1', 'CEPH_NUM_MDS': '1', 'CEPH_NUM_MGR': '1',
        'CEPH_NUM_FS': '1', 'CEPH_NUM_RGW': '0', 'GANESHA_DAEMON_NUM': '0',
        'MGR_PYTHON_PATH': str(base / 'src/pybind/mgr'),
        'PYBIND': str(base / 'src/pybind'),
        'CEPH_PYTHON_COMMON': str(base / 'src/python-common'),
        'LD_LIBRARY_PATH': str(arm / 'lib') + '/:' + str(build / 'lib'),
        'PYTHON': python,
        'PATH': ':'.join([str(Path(python).parent), str(Path(os.environ.get('REPRO_HELPERS', scratch / 'helpers'))),
                          str(arm / 'bin'), env.get('PATH', '')]),
        'PYTHONPATH': ':'.join([str(base / 'src/pybind'), str(base / 'src/python-common'),
                                str(build / 'lib/cython_modules/lib.3')]),
    })
    for key in ('VSTART_DEST', 'CEPH_ARGS', 'CEPH_KEYRING'):
        env.pop(key, None)
    return env


def prepare(arm: Path):
    arm.mkdir()
    (arm / 'CMakeCache.txt').symlink_to(build / 'CMakeCache.txt')
    (arm / 'lib').symlink_to(build / 'lib')
    (arm / 'bin').mkdir()
    for item in (build / 'bin').iterdir():
        target = fault_mds if (fault_mds and item.name == 'ceph-mds') else item
        (arm / 'bin' / item.name).symlink_to(target)
    source = (base / 'src/vstart.sh').read_text()
    old = 'monmap_fn="/tmp/ceph_monmap.$$"'
    new = 'monmap_fn="${TMPDIR:?workspace scratch required}/ceph_monmap.$$"'
    assert source.count(old) == 1
    script = arm / 'vstart-workspace.sh'
    script.write_text(source.replace(old, new))
    script.chmod(0o755)
    for d in ('tmp', 'asok', 'mnt-k'):
        (arm / d).mkdir()


def owned_processes(arm: Path):
    marker = b'CEPH_CONF=' + str(arm / 'ceph.conf').encode()
    found = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / 'cmdline').read_bytes().split(b'\0')
            if argv and Path(os.fsdecode(argv[0])).name in DAEMONS and \
                    marker in (proc / 'environ').read_bytes().split(b'\0'):
                found.append((int(proc.name), os.fsdecode(argv[0])))
        except OSError:
            pass
    return found


def foreign_processes(arm: Path):
    owned = {pid for pid, _ in owned_processes(arm)}
    result = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name) in owned:
            continue
        try:
            argv = (proc / 'cmdline').read_bytes().split(b'\0')
            if argv and Path(os.fsdecode(argv[0])).name in DAEMONS:
                result.append((int(proc.name), os.fsdecode(argv[0])))
        except OSError:
            pass
    return result


def arm_mounts(arm: Path):
    return [line.split()[4] for line in Path('/proc/self/mountinfo').read_text().splitlines()
            if line.split()[4].startswith(str(arm) + '/')]


def start(arm: Path):
    foreign = foreign_processes(arm)
    if foreign:
        raise SystemExit('Refusing: unrelated Ceph daemons running: %s' % foreign)
    if (arm / 'ceph.conf').exists():
        raise SystemExit('Refusing to reinitialize existing arm ' + str(arm))
    env = arm_env(arm)
    opts = []
    for o in ('debug mds = 20', 'debug ms = 1', 'mds log max segments = 1000',
              'bluestore block size = 10737418240',
              'mds_session_autoclose = 3600', 'mds_session_blocklist_on_timeout = false',
              'mds_session_blocklist_on_evict = false'):
        opts += ['-o', o]
    with (arm / 'vstart.log').open('w') as log:
        code = subprocess.call(['bash', str(arm / 'vstart-workspace.sh'), '--new', '--debug',
                                '--localhost', '--bluestore', '--nolockdep'] + opts,
                               cwd=arm, env=env, stdout=log, stderr=subprocess.STDOUT,
                               timeout=900)
    print('vstart exit', code)
    if code:
        raise SystemExit(code)
    out = subprocess.check_output([str(arm / 'bin/ceph'), 'fs', 'status'], cwd=arm, env=env,
                                  text=True, timeout=60)
    print(out)


def stop(arm: Path):
    for m in arm_mounts(arm):
        subprocess.run(['umount', '-f', m], timeout=30)
        subprocess.run(['umount', '-l', m], timeout=30)
    procs = owned_processes(arm)
    (arm / 'owned-processes-at-stop.json').write_text(json.dumps(procs, indent=2) + '\n')

    def send(pid, sig):
        try:
            fd = os.pidfd_open(pid)
            try:
                if pid in [p for p, _ in owned_processes(arm)]:
                    signal.pidfd_send_signal(fd, sig)
            finally:
                os.close(fd)
        except ProcessLookupError:
            pass
    for pid, _ in procs:
        send(pid, signal.SIGTERM)
    deadline = time.time() + 30
    while owned_processes(arm) and time.time() < deadline:
        time.sleep(1)
    for pid, _ in owned_processes(arm):
        send(pid, signal.SIGKILL)
    time.sleep(2)
    left = owned_processes(arm), arm_mounts(arm)
    print('remaining owned processes/mounts:', left)
    if any(left):
        raise SystemExit(1)
    (arm / 'stopped.ok').write_text('No owned daemons or mounts remain.\n')


if __name__ == '__main__':
    action, arm = sys.argv[1], Path(sys.argv[2]).resolve()
    if action == 'env':
        for k, v in arm_env(arm).items():
            if k.startswith(('CEPH', 'LD_', 'PYTHON', 'PATH', 'TMP')):
                print('export %s=%r' % (k, v))
    else:
        {'prepare': prepare, 'start': start, 'stop': stop}[action](arm)
