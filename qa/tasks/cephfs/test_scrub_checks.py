"""
MDS admin socket scrubbing-related tests.
"""
import json
import logging
import errno
import time
import rados
from io import StringIO, BytesIO
from teuthology.exceptions import CommandFailedError
from teuthology.contextutil import safe_while
import os
from tasks.cephfs.cephfs_test_case import CephFSTestCase

log = logging.getLogger(__name__)

class TestScrubControls(CephFSTestCase):
    """
    Test basic scrub control operations such as abort, pause and resume.
    """

    MDSS_REQUIRED = 2
    CLIENTS_REQUIRED = 1

    def _abort_scrub(self, expected):
        res = self.fs.run_scrub(["abort"])
        self.assertEqual(res['return_code'], expected)
    def _pause_scrub(self, expected):
        res = self.fs.run_scrub(["pause"])
        self.assertEqual(res['return_code'], expected)
    def _resume_scrub(self, expected):
        res = self.fs.run_scrub(["resume"])
        self.assertEqual(res['return_code'], expected)
    def _check_task_status(self, expected_status, timo=120):
        """ check scrub status for current active mds in ceph status """
        with safe_while(sleep=1, tries=120, action='wait for task status') as proceed:
            while proceed():
                active = self.fs.get_active_names()
                log.debug("current active={0}".format(active))
                task_status = self.fs.get_task_status("scrub status")
                try:
                    if task_status[active[0]].startswith(expected_status):
                        return True
                except KeyError:
                    pass

    def _check_task_status_na(self, timo=120):
        """ check absence of scrub status in ceph status """
        with safe_while(sleep=1, tries=120, action='wait for task status') as proceed:
            while proceed():
                active = self.fs.get_active_names()
                log.debug("current active={0}".format(active))
                task_status = self.fs.get_task_status("scrub status")
                if not active[0] in task_status:
                    return True

    def create_scrub_data(self, test_dir):
        for i in range(32):
            dirname = "dir.{0}".format(i)
            dirpath = os.path.join(test_dir, dirname)
            self.mount_a.run_shell_payload(f"""
set -e
mkdir -p {dirpath}
for ((i = 0; i < 32; i++)); do
    dd if=/dev/urandom of={dirpath}/filename.$i bs=1M conv=fdatasync count=1
done
""")

    def test_scrub_abort(self):
        test_dir = "scrub_control_test_path"
        abs_test_path = "/{0}".format(test_dir)

        self.create_scrub_data(test_dir)

        out_json = self.fs.run_scrub(["start", abs_test_path, "recursive"])
        self.assertNotEqual(out_json, None)

        # abort and verify
        self._abort_scrub(0)
        self.fs.wait_until_scrub_complete(sleep=5, timeout=30)

        # sleep enough to fetch updated task status
        checked = self._check_task_status_na()
        self.assertTrue(checked)

    def test_scrub_pause_and_resume(self):
        test_dir = "scrub_control_test_path"
        abs_test_path = "/{0}".format(test_dir)

        log.info("mountpoint: {0}".format(self.mount_a.mountpoint))
        client_path = os.path.join(self.mount_a.mountpoint, test_dir)
        log.info("client_path: {0}".format(client_path))

        self.create_scrub_data(test_dir)

        out_json = self.fs.run_scrub(["start", abs_test_path, "recursive"])
        self.assertNotEqual(out_json, None)

        # pause and verify
        self._pause_scrub(0)
        out_json = self.fs.get_scrub_status()
        self.assertTrue("PAUSED" in out_json['status'])

        checked = self._check_task_status("paused")
        self.assertTrue(checked)

        # resume and verify
        self._resume_scrub(0)
        out_json = self.fs.get_scrub_status()
        self.assertFalse("PAUSED" in out_json['status'])

        checked = self._check_task_status_na()
        self.assertTrue(checked)

    def test_scrub_pause_and_resume_with_abort(self):
        test_dir = "scrub_control_test_path"
        abs_test_path = "/{0}".format(test_dir)

        self.create_scrub_data(test_dir)

        out_json = self.fs.run_scrub(["start", abs_test_path, "recursive"])
        self.assertNotEqual(out_json, None)

        # pause and verify
        self._pause_scrub(0)
        out_json = self.fs.get_scrub_status()
        self.assertTrue("PAUSED" in out_json['status'])

        checked = self._check_task_status("paused")
        self.assertTrue(checked)

        # abort and verify
        self._abort_scrub(0)
        out_json = self.fs.get_scrub_status()
        self.assertTrue("PAUSED" in out_json['status'])
        self.assertTrue("0 inodes" in out_json['status'])

        # scrub status should still be paused...
        checked = self._check_task_status("paused")
        self.assertTrue(checked)

        # resume and verify
        self._resume_scrub(0)
        self.assertTrue(self.fs.wait_until_scrub_complete(sleep=5, timeout=30))

        checked = self._check_task_status_na()
        self.assertTrue(checked)

    def test_scrub_task_status_on_mds_failover(self):
        (original_active, ) = self.fs.get_active_names()
        original_standbys = self.mds_cluster.get_standby_daemons()

        test_dir = "scrub_control_test_path"
        abs_test_path = "/{0}".format(test_dir)

        self.create_scrub_data(test_dir)

        out_json = self.fs.run_scrub(["start", abs_test_path, "recursive"])
        self.assertNotEqual(out_json, None)

        # pause and verify
        self._pause_scrub(0)
        out_json = self.fs.get_scrub_status()
        self.assertTrue("PAUSED" in out_json['status'])

        checked = self._check_task_status("paused")
        self.assertTrue(checked)

        # Kill the rank 0
        self.fs.mds_stop(original_active)

        def promoted():
            active = self.fs.get_active_names()
            return active and active[0] in original_standbys

        log.info("Waiting for promotion of one of the original standbys {0}".format(
            original_standbys))
        self.wait_until_true(promoted, timeout=self.fs.beacon_timeout)

        self._check_task_status_na()

class TestScrubChecks(CephFSTestCase):
    """
    Run flush and scrub commands on the specified files in the filesystem. This
    task will run through a sequence of operations, but it is not comprehensive
    on its own -- it doesn't manipulate the mds cache state to test on both
    in- and out-of-memory parts of the hierarchy. So it's designed to be run
    multiple times within a single test run, so that the test can manipulate
    memory state.

    Usage:
    mds_scrub_checks:
      mds_rank: 0
      path: path/to/test/dir
      client: 0
      run_seq: [0-9]+

    Increment the run_seq on subsequent invocations within a single test run;
    it uses that value to generate unique folder and file names.
    """

    MDSS_REQUIRED = 1
    CLIENTS_REQUIRED = 1

    def test_scrub_checks(self):
        self._checks(0)
        self._checks(1)

    def _checks(self, run_seq):
        mds_rank = 0
        test_dir = "scrub_test_path"

        abs_test_path = "/{0}".format(test_dir)

        log.info("mountpoint: {0}".format(self.mount_a.mountpoint))
        client_path = os.path.join(self.mount_a.mountpoint, test_dir)
        log.info("client_path: {0}".format(client_path))

        log.info("Cloning repo into place")
        repo_path = TestScrubChecks.clone_repo(self.mount_a, client_path)

        log.info("Initiating mds_scrub_checks on mds.{id_} test_path {path}, run_seq {seq}".format(
            id_=mds_rank, path=abs_test_path, seq=run_seq)
        )


        success_validator = lambda j, r: self.json_validator(j, r, "return_code", 0)

        nep = "{test_path}/i/dont/exist".format(test_path=abs_test_path)
        self.asok_command(mds_rank, "flush_path {nep}".format(nep=nep),
                          lambda j, r: self.json_validator(j, r, "return_code", -errno.ENOENT))
        self.tell_command(mds_rank, "scrub start {nep}".format(nep=nep),
                          lambda j, r: self.json_validator(j, r, "return_code", -errno.ENOENT))

        test_repo_path = "{test_path}/ceph-qa-suite".format(test_path=abs_test_path)
        dirpath = "{repo_path}/suites".format(repo_path=test_repo_path)

        if run_seq == 0:
            log.info("First run: flushing {dirpath}".format(dirpath=dirpath))
            command = "flush_path {dirpath}".format(dirpath=dirpath)
            self.asok_command(mds_rank, command, success_validator)
        command = "scrub start {dirpath}".format(dirpath=dirpath)
        self.tell_command(mds_rank, command, success_validator)

        filepath = "{repo_path}/suites/fs/verify/validater/valgrind.yaml".format(
            repo_path=test_repo_path)
        if run_seq == 0:
            log.info("First run: flushing {filepath}".format(filepath=filepath))
            command = "flush_path {filepath}".format(filepath=filepath)
            self.asok_command(mds_rank, command, success_validator)
        command = "scrub start {filepath}".format(filepath=filepath)
        self.tell_command(mds_rank, command, success_validator)

        if run_seq == 0:
            log.info("First run: flushing base dir /")
            command = "flush_path /"
            self.asok_command(mds_rank, command, success_validator)
        command = "scrub start /"
        self.tell_command(mds_rank, command, success_validator)

        new_dir = "{repo_path}/new_dir_{i}".format(repo_path=repo_path, i=run_seq)
        test_new_dir = "{repo_path}/new_dir_{i}".format(repo_path=test_repo_path,
                                                        i=run_seq)
        self.mount_a.run_shell(["mkdir", new_dir])
        command = "flush_path {dir}".format(dir=test_new_dir)
        self.asok_command(mds_rank, command, success_validator)

        new_file = "{repo_path}/new_file_{i}".format(repo_path=repo_path,
                                                     i=run_seq)
        test_new_file = "{repo_path}/new_file_{i}".format(repo_path=test_repo_path,
                                                          i=run_seq)
        self.mount_a.write_n_mb(new_file, 1)

        command = "flush_path {file}".format(file=test_new_file)
        self.asok_command(mds_rank, command, success_validator)

        # check that scrub fails on errors
        ino = self.mount_a.path_to_ino(new_file)
        rados_obj_name = "{ino:x}.00000000".format(ino=ino)
        command = "scrub start {file}".format(file=test_new_file)

        def _check_and_clear_damage(ino, dtype):
            all_damage = self.fs.rank_tell(["damage", "ls"], mds_rank)
            damage = [d for d in all_damage if d['ino'] == ino and d['damage_type'] == dtype]
            for d in damage:
                self.run_ceph_cmd(
                    'tell', f'mds.{self.fs.get_active_names()[mds_rank]}',
                    "damage", "rm", str(d['id']))
            return len(damage) > 0

        # Missing parent xattr
        self.assertFalse(_check_and_clear_damage(ino, "backtrace"));
        self.fs.rados(["rmxattr", rados_obj_name, "parent"], pool=self.fs.get_data_pool_name())
        self.tell_command(mds_rank, command, success_validator)
        self.fs.wait_until_scrub_complete(sleep=5, timeout=30)
        self.assertTrue(_check_and_clear_damage(ino, "backtrace"));

        command = "flush_path /"
        self.asok_command(mds_rank, command, success_validator)

    def scrub_with_stray_evaluation(self, fs, mnt, path, flag, files=2000,
                                    _hard_links=3):
        fs.set_allow_new_snaps(True)

        test_dir = "stray_eval_dir"
        mnt.run_shell(["mkdir", test_dir])
        client_path = os.path.join(mnt.mountpoint, test_dir)
        mnt.create_n_files(fs_path=f"{test_dir}/file", count=files,
                           hard_links=_hard_links)
        mnt.run_shell(["mkdir", f"{client_path}/.snap/snap1-{test_dir}"])
        mnt.run_shell(f"find {client_path}/ -type f -delete")
        mnt.run_shell(["rmdir", f"{client_path}/.snap/snap1-{test_dir}"])
        perf_dump = fs.rank_tell(["perf", "dump"], 0)
        self.assertNotEqual(perf_dump.get('mds_cache').get('num_strays'),
                            0, "mdcache.num_strays is zero")

        log.info(
            f"num of strays: {perf_dump.get('mds_cache').get('num_strays')}")

        out_json = fs.run_scrub(["start", path, flag])
        self.assertNotEqual(out_json, None)
        self.assertEqual(out_json["return_code"], 0)

        self.assertEqual(
            fs.wait_until_scrub_complete(tag=out_json["scrub_tag"]), True)

        perf_dump = fs.rank_tell(["perf", "dump"], 0)
        self.assertEqual(int(perf_dump.get('mds_cache').get('num_strays')),
                         0, "mdcache.num_strays is non-zero")

    def test_scrub_repair(self):
        mds_rank = 0
        test_dir = "scrub_repair_path"

        self.mount_a.run_shell(["mkdir", test_dir])
        self.mount_a.run_shell(["touch", "{0}/file".format(test_dir)])
        dir_objname = "{:x}.00000000".format(self.mount_a.path_to_ino(test_dir))

        self.mount_a.umount_wait()

        # flush journal entries to dirfrag objects, and expire journal
        self.fs.mds_asok(['flush', 'journal'])
        self.fs.mds_stop()

        # remove the dentry from dirfrag, cause incorrect fragstat/rstat
        self.fs.radosm(["rmomapkey", dir_objname, "file_head"])

        self.fs.mds_fail_restart()
        self.fs.wait_for_daemons()

        self.mount_a.mount_wait()

        # fragstat indicates the directory is not empty, rmdir should fail
        with self.assertRaises(CommandFailedError) as ar:
            self.mount_a.run_shell(["rmdir", test_dir])
        self.assertEqual(ar.exception.exitstatus, 1)

        self.tell_command(mds_rank, "scrub start /{0} repair".format(test_dir),
                          lambda j, r: self.json_validator(j, r, "return_code", 0))

        # wait a few second for background repair
        time.sleep(10)

        # fragstat should be fixed
        self.mount_a.run_shell(["rmdir", test_dir])

    def test_scrub_remote_link(self):
        """
        test scrub remote link
        """
        test_dir_path1 = "test_dir1"
        test_dir_path2 = "test_dir2"
        self.mount_a.run_shell(["mkdir", test_dir_path1])
        self.mount_a.run_shell(["mkdir", test_dir_path2])

        self.run_ceph_cmd("config", "set", "global", "mds_log_max_segments", "128")
        self.fs.mds_asok(['config', 'set', 'mds_log_max_segments', '128'])

        # Filesystem layout: n levels, b-way branching; parent is e_(level-1)_(serial//b)
        n = 5
        b = 10

        # Build the absolute path for e_<level>_<serial>
        def build_entry_path(base_dir, level, serial):
            path_part = ""
            cur_serial = serial
            for cur_level in range(level, -1, -1):
                name = f"e_{cur_level}_{cur_serial}"
                if path_part:
                    path_part = os.path.join(name, path_part)
                else:
                    path_part = name
                cur_serial //= b
            return os.path.join(base_dir, path_part)

        def create_tree(base_dir):
            # Create all directory entries for levels 0..n-1
            level0_path = build_entry_path(base_dir, 0, 0)
            self.mount_a.run_shell(["mkdir", level0_path])

            for level in range(1, n):
                count = b ** level
                for serial in range(count):
                    entry_path = build_entry_path(base_dir, level, serial)
                    self.mount_a.run_shell(["mkdir", entry_path])

            # Hardlink strategy: e_n_(i * part_span + x)--> e_n_((i + 1) * part_span + x)
            # e.g. e_n_0->e_n_10000->e_n_20000->e_n_30000->e_n_40000...e_n_90000
            # e_n_[90000...99999] are all files and e_n_[0...89999] are all hardlinks
            part_span = b ** (n - 1)

            for x in range(part_span):
                print(f"creating link chain-{x}")
                serial_last = (b - 1) * part_span + x
                file_path_candidate = build_entry_path(base_dir, n, serial_last)
                self.mount_a.run_shell(["touch", file_path_candidate])
                prev_path = file_path_candidate
                for t in range(b - 2, -1, -1):
                    serial = t * part_span + x
                    link_path_candidate = build_entry_path(base_dir, n, serial)
                    self.mount_a.run_shell(["ln", prev_path, link_path_candidate])
                    prev_path = link_path_candidate
            return part_span

        part_span = create_tree(test_dir_path1)
        # Goal of this creating identical directory like test_dir_path1
        # is, we will do all the destructive operation on test_dir_path1
        # and make sure test_dir_path2 is intact
        create_tree(test_dir_path2)

        self.run_ceph_cmd("config", "set", "global", "mds_damage_table_max_entries", "1000000")
        self.run_ceph_cmd("config", "set", "global", "mds_force_hard_link_scrubbing", "true")

        # Flush to persist objects before damage injection
        self.fs.flush()

        meta_pool = self.fs.get_metadata_pool_name()
        data_pool = self.fs.get_data_pool_name()

        conf = self.mount_a.config_path
        keyring = self.mount_a.get_keyring_path()
        cluster = rados.Rados(conffile=conf,
                              name="client.admin",
                              conf={"keyring": keyring})
        cluster.connect()
        meta_ioctx = cluster.open_ioctx(meta_pool)
        data_ioctx = cluster.open_ioctx(data_pool)
        mds_rank = self.fs.get_rank()['rank']
        self.inject_damage(
            test_dir_path1, build_entry_path, n, b,
            meta_ioctx, data_ioctx)
        remote_link_entries = self.verify_remote_link_damage(
            mds_rank, test_dir_path1, build_entry_path, n, b, part_span)
        self.fix_remote_link_damage(
            remote_link_entries, mds_rank, test_dir_path1, meta_ioctx)

        success_validator = lambda j, r: self.json_validator(j, r, "return_code", 0)
        scrub_json = self.tell_command(
            mds_rank, "scrub start / recursive scrub_mdsdir", success_validator)
        self.assertEqual(
            self.fs.wait_until_scrub_complete(tag=scrub_json["scrub_tag"], sleep=5), True)

        self.verify_remote_link_existence(mds_rank, test_dir_path1)


        self.verify_nlink_counts(test_dir_path1)
        self.verify_nlink_counts(test_dir_path2)


        self.remove_test_dir_and_verify(test_dir_path1, meta_ioctx, data_ioctx)
        self.remove_test_dir_and_verify(test_dir_path2, meta_ioctx, data_ioctx)

        meta_ioctx.close()
        data_ioctx.close()
        cluster.shutdown()


    def fix_remote_link_damage(self, remote_link_entries, mds_rank,
                                test_dir_path, meta_ioctx):
        self.failover_and_cleanup(meta_ioctx, False, True)
        self.restart_mds()

        success_validator = lambda j, r: self.json_validator(j, r, "return_code", 0)
        scrub_json = self.tell_command(
            mds_rank,
            "scrub start {0} recursive repair force".format(
                test_dir_path if test_dir_path.startswith("/") else "/" + test_dir_path),
            success_validator)
        self.assertEqual(
            self.fs.wait_until_scrub_complete(tag=scrub_json["scrub_tag"], sleep=5), True)

        self.failover_and_cleanup(meta_ioctx, True, True)
        self.restart_mds()

        success_validator = lambda j, r: self.json_validator(j, r, "return_code", 0)
        scrub_json = self.tell_command(
            mds_rank,
            "scrub start {0} recursive force".format(
                test_dir_path if test_dir_path.startswith("/") else "/" + test_dir_path),
            success_validator)
        self.assertEqual(
            self.fs.wait_until_scrub_complete(tag=scrub_json["scrub_tag"], sleep=5), True)
        damage_json = self.tell_command(mds_rank, "damage ls")

        remote_link_entries = {
            entry["path"]: entry for entry in damage_json
            if entry.get("damage_type") == "remote_link"
        }


        self.failover_and_cleanup(meta_ioctx, True, True)
        missing_parent_inos = []
        missing_omap_keys = []
        bad_root_parents = []
        missing_parent_dirfrags = []
        total_links = len(remote_link_entries)
        for idx, (link_path, entry) in enumerate(remote_link_entries.items(), start=1):
            if idx % 10 == 0 or idx == total_links:
                log.info(
                    "fix_remote_link_damage: remote_link parent check %d/%d",
                    idx, total_links)
            parent_ino = entry.get("parent_ino")
            if parent_ino is None:
                missing_parent_inos.append(link_path)
                continue
            if parent_ino == 0:
                parent_dir = os.path.dirname(link_path)
                if parent_dir != "/":
                    bad_root_parents.append(link_path)
                continue
            key = "{0}_head".format(os.path.basename(link_path))
            dirfrag_obj_name = "{0:x}.00000000".format(parent_ino)
            try:
                meta_ioctx.stat(dirfrag_obj_name)
            except rados.ObjectNotFound:
                missing_parent_dirfrags.append(link_path)
                continue
            with rados.ReadOpCtx() as read_op:
                it, _ = meta_ioctx.get_omap_vals_by_keys(read_op, (key,), omap_key_type=bytes)
                meta_ioctx.operate_read_op(read_op, dirfrag_obj_name)
                omap_vals = list(it)
            if not omap_vals:
                missing_omap_keys.append(link_path)
                continue
            with rados.WriteOpCtx() as write_op:
                meta_ioctx.remove_omap_keys(write_op, (key,))
                meta_ioctx.operate_write_op(write_op, dirfrag_obj_name)
        if missing_parent_inos:
            self.fail("missing parent_ino in remote_link damage entries: {0}".format(
                ", ".join(missing_parent_inos)))
        if bad_root_parents:
            self.fail("parent_ino is 0 but parent path is not '/': {0}".format(
                ", ".join(bad_root_parents)))
        if missing_parent_dirfrags:
            self.fail("missing parent dirfrag objects for remote_link entries: {0}".format(
                ", ".join(missing_parent_dirfrags)))
        if missing_omap_keys:
            self.fail("missing _head omap keys for remote_link parents: {0}".format(
                ", ".join(missing_omap_keys)))
        self.restart_mds()

        success_validator = lambda j, r: self.json_validator(j, r, "return_code", 0)
        scrub_json = self.tell_command(
            mds_rank,
            "scrub start {0} recursive repair force".format(
                test_dir_path if test_dir_path.startswith("/") else "/" + test_dir_path),
            success_validator)
        self.assertEqual(
            self.fs.wait_until_scrub_complete(tag=scrub_json["scrub_tag"], sleep=5), True)
        damage_json = self.tell_command(mds_rank, "damage ls")
        self.failover_and_cleanup(meta_ioctx, True, True)
        self.restart_mds()


    def failover_and_cleanup(self, meta_ioctx, flush_jounrnal, cleanup):
        if flush_jounrnal:
            self.fs.mds_asok(['flush', 'journal'])
        self.fs.fail()
        if cleanup:
            self.remove_openfiles_objects(meta_ioctx)
            self.fs.journal_tool(["journal", "reset"], 0)

    def restart_mds(self):
        self.fs.mds_fail_restart()
        self.fs.set_down(False)
        self.fs.set_joinable(True)
        self.fs.wait_for_daemons()
        status = self.fs.mds_asok(["status"])
        self.assertEqual("up:active", str(status["state"]))
        self.mount_a._run_umount_lf()
        self.mount_a.mount_wait()

    def verify_remote_link_existence(self, mds_rank, test_dir_path):
        log.info(
            "verify_remote_link_existence: clearing damage and verifying "
            "no remote_link entries under %s", test_dir_path)
        self.fs.mds_asok(["damage", "clear"])
        success_validator = lambda j, r: self.json_validator(j, r, "return_code", 0)
        scrub_json = self.tell_command(
            mds_rank,
            "scrub start {0} recursive force".format(
                test_dir_path if test_dir_path.startswith("/") else "/" + test_dir_path),
            success_validator)
        self.assertEqual(
            self.fs.wait_until_scrub_complete(tag=scrub_json["scrub_tag"], sleep=5), True)
        damage_json = self.tell_command(mds_rank, "damage ls")

        for entry in damage_json:
            if entry.get("damage_type") == "remote_link":
                self.fail("unexpected remote_link damage after clear: {0}".format(entry.get("path")))
        
        self.fs.mds_asok(["damage", "clear"])
        # scrub_json = self.tell_command(
        #     mds_rank,
        #     "scrub start {0} recursive repair force".format(
        #         test_dir_path if test_dir_path.startswith("/") else "/" + test_dir_path),
        #     success_validator)
        # self.fs.mds_asok(["damage", "clear"])
        # scrub_json = self.tell_command(
        #     mds_rank,
        #     "scrub start {0} recursive force".format(
        #         test_dir_path if test_dir_path.startswith("/") else "/" + test_dir_path),
        #     success_validator)
        # damage_json = self.tell_command(mds_rank, "damage ls")
        self.fs.mds_asok(['flush', 'journal'])

    def verify_nlink_counts(self, test_dir_path):
        log.info(
            "verify_nlink_counts: verifying nlink counts under %s", test_dir_path)

        out = self.mount_a.run_shell([
            "find", test_dir_path, "-printf", "%i %y %n %p\n"
        ], stdout=StringIO()).stdout.getvalue()

        file_inode_map = {}
        file_nlink_map = {}
        dir_nlink_map = {}
        dir_paths = set()
        dir_parent_counts = {}

        for line in out.splitlines():
            if not line:
                continue
            parts = line.split(" ", 3)
            if len(parts) < 4:
                continue
            inode, ftype, nlink, path_str = parts
            if ftype == "f":
                file_inode_map.setdefault(inode, set()).add(os.path.basename(path_str))
                file_nlink_map[inode] = nlink
            elif ftype == "d":
                dir_paths.add(path_str)
                dir_nlink_map[path_str] = nlink

        for dir_path in dir_paths:
            if dir_path == test_dir_path:
                continue
            parent = os.path.dirname(dir_path)
            if parent in dir_paths:
                dir_parent_counts[parent] = dir_parent_counts.get(parent, 0) + 1

        file_mismatches = []
        log.info(
            "verify_nlink_counts: nlink check files: %d",
            len(file_inode_map.items()))
        for inode, names in file_inode_map.items():
            actual = int(file_nlink_map.get(inode))
            expected = len(names)
            log.info(
                "verify_nlink_counts: file inode %s nlink=%s expected=%s",
                inode, actual, expected)
            log.info(
                "verify_nlink_counts: file inode %s paths: %s",
                inode, ", ".join(sorted(names)))
            if actual != expected:
                file_mismatches.append((inode, expected, actual))
        if file_mismatches:
            details = ", ".join(
                "{0}: expected {1}, got {2}".format(i, e, a)
                for i, e, a in file_mismatches)
            self.fail("nlink mismatch for files: {0}".format(details))

        log.info(
            "verify_nlink_counts: nlink check directories: %d",
            len(dir_paths))
        dir_mismatches = []
        for dir_path in dir_paths:
            expected_subdirs = dir_parent_counts.get(dir_path, 0)
            expected_nlink = 2 + expected_subdirs
            actual = dir_nlink_map.get(dir_path)
            log.info(
                "verify_nlink_counts: nlink check %s: expected %d, got %s",
                dir_path, expected_nlink, actual)
            if int(actual) != expected_nlink:
                dir_mismatches.append((dir_path, expected_nlink, actual))
        if dir_mismatches:
            details = ", ".join(
                "{0}: expected {1}, got {2}".format(p, e, a)
                for p, e, a in dir_mismatches)
            self.fail("nlink mismatch for directories: {0}".format(details))

    def remove_test_dir_and_verify(self, test_dir_path, meta_ioctx, data_ioctx):
        log.info(
            "remove_test_dir_and_verify: removing test directory %s and "
            "waiting for purges", test_dir_path)


        count_out = self.mount_a.run_shell([
            "find", test_dir_path, "-printf", "%i %y %p\n"
        ], stdout=StringIO()).stdout.getvalue()
        inode_types = {}
        inode_paths = {}
        for line in count_out.splitlines():
            if not line:
                continue
            parts = line.split(" ", 2)
            if len(parts) < 3:
                continue
            inode_types.setdefault(parts[0], parts[1])
            inode_paths.setdefault(parts[0], parts[2])

        expected_purges = 0
        for inode, ftype in inode_types.items():
            obj = "{0:x}.00000000".format(int(inode))
            ioctx = meta_ioctx if ftype == "d" else data_ioctx
            try:
                ioctx.stat(obj)
            except rados.ObjectNotFound:
                log.info(
                    "remove_test_dir_and_verify: inode object missing: %s path=%s",
                    inode, inode_paths.get(inode))
                continue
            expected_purges += 1

        log.info(
            "remove_test_dir_and_verify: expected purges (existing inodes): %d",
            expected_purges)

        self.fs.mds_asok(["flush", "journal"])
        
        def strays_cleared():
            num_strays = self.fs.mds_asok(
                ["perf", "dump", "mds_cache"])["mds_cache"]["num_strays"]
            pq_ops = self.fs.mds_asok(
                ["perf", "dump", "purge_queue"])["purge_queue"]["pq_executing"]
            log.info(
                "remove_test_dir_and_verify: num_strays=%d pq_executing=%d",
                num_strays, pq_ops)
            return num_strays == 0 and pq_ops == 0

        self.wait_until_true(strays_cleared, timeout=60)

        initial_deletes = self.fs.mds_asok(["perf", "dump", "objecter"])["objecter"]["osdop_delete"]

        self.mount_a.run_shell(["rm", "-rf", test_dir_path])
        with self.assertRaises(CommandFailedError):
            self.mount_a.stat(test_dir_path)
        self.fs.mds_asok(["flush", "journal"])

        def delete_progress():
            return (self.fs.mds_asok(["perf", "dump", "objecter"])["objecter"]["osdop_delete"]
                    - initial_deletes)

        def progress_reached():
            current = delete_progress()
            log.info(
                "remove_test_dir_and_verify: osdop_delete progress (after 5s): %d/%d",
                current, expected_purges)
            return current >= expected_purges

        self.wait_until_true(progress_reached, timeout=60, period=5)

        for inode, ftype in inode_types.items():
            obj = "{0:x}.00000000".format(int(inode))
            ioctx = meta_ioctx if ftype == "d" else data_ioctx
            try:
                ioctx.stat(obj)
            except rados.ObjectNotFound:
                continue
            self.fail(
                "remove_test_dir_and_verify: purge still present: "
                "inode={0} path={1}".format(
                    inode, inode_paths.get(inode)))

        status = self.fs.mds_asok(["status"])
        self.assertEqual("up:active", str(status["state"]))


    def inject_damage(self, test_dir_path, build_entry_path, n, b,
                                        meta_ioctx, data_ioctx):
        # goal is to make all files in-accessible through path finding
        # for e_n_[90000...90999] removed e_2_90's metadata from disk
        # for e_n_[91000...91999] removed e_3_[910...919]'s metadata from disk
        # for e_n_[92000...92999] removed e_4_[9200...9299]'s metadata from disk
        # so on...
        for i in range(b):
            level = i + 2
            if level >= n:
                level = n
            y = (b - 1) * b + i
            base = b ** (level - 2)
            ioctx = data_ioctx if level == n else meta_ioctx
            for x in range(base):
                serial = y * base + x
                print(f"removing directory e_{level}_{serial}'s metadata")
                entry_path = build_entry_path(test_dir_path, level, serial)
                ino = self.mount_a.path_to_ino(entry_path)
                rados_obj = "{ino:x}.00000000".format(ino=ino)
                ioctx.remove_object(rados_obj)


    def verify_remote_link_damage(self, mds_rank, test_dir_path, build_entry_path, n, b, part_span):
        status = self.fs.mds_asok(["status"])
        self.assertEqual("up:active", str(status["state"]))

        success_validator = lambda j, r: self.json_validator(j, r, "return_code", 0)
        scrub_json = self.tell_command(
            mds_rank,
            "scrub start {0} recursive force".format(
                test_dir_path if test_dir_path.startswith("/") else "/" + test_dir_path),
            success_validator)
        self.assertEqual(
            self.fs.wait_until_scrub_complete(tag=scrub_json["scrub_tag"], sleep=5), True)

        damage_json = self.tell_command(mds_rank, "damage ls")
        remote_link_paths = set(
            entry["path"] for entry in damage_json
            if entry.get("damage_type") == "remote_link")
        remote_link_entries = {
            entry["path"]: entry for entry in damage_json
            if entry.get("damage_type") == "remote_link"
        }
        missing_links = []
        hard_link_serial_range = part_span * (b - 1)
        for serial in range(hard_link_serial_range):
            link_path = "/" + build_entry_path(test_dir_path, n, serial)
            if link_path not in remote_link_paths:
                missing_links.append(link_path)
        if missing_links:
            self.fail("missing remote_link damage entries: {0}".format(
                ", ".join(missing_links)))
        return remote_link_entries

    def remove_openfiles_objects(self, meta_ioctx):
        log.info("removing openfiles")
        x = 0
        while True:
            obj_name = "mds0_openfiles.{0}".format(x)
            try:
                meta_ioctx.stat(obj_name)
            except rados.ObjectNotFound:
                break
            log.info(
                "remove_openfiles_objects: removing %s", obj_name)
            meta_ioctx.remove_object(obj_name)
            x += 1

    def test_stray_evaluation_with_scrub(self):
        """
        test that scrub can iterate over ~mdsdir and evaluate strays
        """
        self.scrub_with_stray_evaluation(self.fs, self.mount_a, "~mdsdir",
                                         "recursive")

    def test_flag_scrub_mdsdir(self):
        """
        test flag scrub_mdsdir
        """
        self.scrub_with_stray_evaluation(self.fs, self.mount_a, "/",
                                         "recursive,scrub_mdsdir")

    @staticmethod
    def json_validator(json_out, rc, element, expected_value):
        if rc != 0:
            return False, "asok command returned error {rc}".format(rc=rc)
        element_value = json_out.get(element)
        if element_value != expected_value:
            return False, "unexpectedly got {jv} instead of {ev}!".format(
                jv=element_value, ev=expected_value)
        return True, "Succeeded"

    def tell_command(self, mds_rank, command, validator=None):
        log.info("Running command '{command}'".format(command=command))

        command_list = command.split()
        jout = self.fs.rank_tell(command_list, mds_rank)

        log.info("command '{command}' returned '{jout}'".format(
                     command=command, jout=jout))

        if validator:
            success, errstring = validator(jout, 0)
            if not success:
                raise AsokCommandFailedError(command, 0, jout, errstring)
        return jout

    def asok_command(self, mds_rank, command, validator):
        log.info("Running command '{command}'".format(command=command))

        command_list = command.split()

        # we just assume there's an active mds for every rank
        mds_id = self.fs.get_active_names()[mds_rank]
        proc = self.fs.mon_manager.admin_socket('mds', mds_id,
                                                command_list, check_status=False)
        rout = proc.exitstatus
        sout = proc.stdout.getvalue()

        if sout.strip():
            jout = json.loads(sout)
        else:
            jout = None

        log.info("command '{command}' got response code '{rout}' and stdout '{sout}'".format(
            command=command, rout=rout, sout=sout))

        success, errstring = validator(jout, rout)

        if not success:
            raise AsokCommandFailedError(command, rout, jout, errstring)

        return jout

    @staticmethod
    def clone_repo(client_mount, path):
        repo = "ceph-qa-suite"
        repo_path = os.path.join(path, repo)
        client_mount.run_shell(["mkdir", "-p", path])

        try:
            client_mount.stat(repo_path)
        except CommandFailedError:
            client_mount.run_shell([
                "git", "clone", '--branch', 'giant',
                "http://github.com/ceph/{repo}".format(repo=repo),
                "{path}/{repo}".format(path=path, repo=repo)
            ])

        return repo_path


class AsokCommandFailedError(Exception):
    """
    Exception thrown when we get an unexpected response
    on an admin socket command
    """

    def __init__(self, command, rc, json_out, errstring):
        self.command = command
        self.rc = rc
        self.json = json_out
        self.errstring = errstring

    def __str__(self):
        return "Admin socket: {command} failed with rc={rc} json output={json}, because '{es}'".format(
            command=self.command, rc=self.rc, json=self.json, es=self.errstring)
