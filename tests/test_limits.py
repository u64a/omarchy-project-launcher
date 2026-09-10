from __future__ import annotations

import errno
import io
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src import omarchy_project_launcher as launcher


ROOT = Path(__file__).resolve().parents[1]


class ResourceLimitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / "Projects"
        self.root.mkdir()

    def repo(self, name="source"):
        path = self.base / name
        launcher.run_command(["git", "init", "--quiet", "-b", "main", str(path)])
        return path

    def test_both_streams_are_drained_and_capped_before_decoding(self):
        result = launcher.run_command(
            [sys.executable, "-c", "import os; os.write(1,b'abc'); os.write(2,b'def')"],
            stdout_limit=3, stderr_limit=3,
        )
        self.assertEqual((result.stdout, result.stderr), ("abc", "def"))
        for fd, message in ((1, "stdout"), (2, "stderr")):
            with self.subTest(fd=fd), self.assertRaisesRegex(launcher.ProjectOperationError, message):
                launcher.run_command(
                    [sys.executable, "-c", f"import os; os.write({fd},b'\\xff'*65)"],
                    stdout_limit=64, stderr_limit=64,
                )

    def test_runtime_is_bounded_without_any_output(self):
        start = time.monotonic()
        with self.assertRaisesRegex(launcher.ProjectOperationError, "time limit"):
            launcher.run_command([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)
        self.assertLess(time.monotonic() - start, 2)

    def test_descendants_with_inherited_pipes_are_killed_and_reaped(self):
        marker = self.base / "pid"
        child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(10)"
        code = (
            "import subprocess,sys; from pathlib import Path; "
            f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
            f"Path({str(marker)!r}).write_text(str(p.pid))"
        )
        with self.assertRaisesRegex(launcher.ProjectOperationError, "time limit"):
            launcher.run_command([sys.executable, "-c", code], timeout=0.2)
        pid = int(marker.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def test_success_does_not_leave_descendants_with_closed_pipes(self):
        marker = self.base / "pid"
        code = (
            "import subprocess,sys; from pathlib import Path; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)'],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            f"Path({str(marker)!r}).write_text(str(p.pid))"
        )
        launcher.run_command([sys.executable, "-c", code])
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

    def test_nonzero_errors_do_not_echo_unbounded_command_or_stderr(self):
        with patch.object(launcher, "MAX_ERROR", 32):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                launcher.run_command(
                    [sys.executable, "-c", "import sys; sys.stderr.write('x'*100); sys.exit(2)"]
                )
            self.assertLessEqual(len(raised.exception.stderr), 32)
            self.assertEqual(raised.exception.cmd, [sys.executable])

    def test_failed_and_oversized_config_scans_fail_closed(self):
        source = self.repo()
        for failure in (
            subprocess.CalledProcessError(1, ["git"], stderr="bad config"),
            launcher.ProjectOperationError("config exceeded byte limit"),
            OSError("cannot read config"),
        ):
            commands = []

            def runner(command):
                commands.append(command)
                raise failure

            with self.subTest(failure=failure):
                inspected = launcher.inspect_repository(source, runner)
                self.assertIn("Cannot verify Git config", inspected.error)
                self.assertNotIn("clean", inspected.status_text)
                self.assertEqual(len(commands), 1)
                with self.assertRaises(launcher.ProjectOperationError):
                    launcher.import_project(self.root, source, runner=runner)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_real_config_does_not_run_status(self):
        source = self.repo()
        (source / ".git" / "config").write_text("[invalid\n")
        self.assertIn("Cannot verify Git config", launcher.inspect_repository(source).error)
        with self.assertRaises(launcher.ProjectOperationError):
            launcher.import_project(self.root, source)

    def test_record_and_line_limits_mark_status_unknown(self):
        def runner(command):
            output = "? one\n? two\n? three\n" if "status" in command else ""
            return subprocess.CompletedProcess(command, 0, output, "")

        with patch.object(launcher, "MAX_RECORDS", 2):
            self.assertIn("record limit", launcher.inspect_repository(Path("example"), runner).error)
        with patch.object(launcher, "MAX_LINE", 2):
            self.assertIn("line limit", launcher.inspect_repository(Path("example"), runner).error)
        with patch.object(launcher, "MAX_RECORDS", 2):
            with self.assertRaises(launcher.ProjectOperationError):
                launcher.executable_config_keys(Path("example"), lambda c: subprocess.CompletedProcess(
                    c, 0, "one.key\ntwo.key\nthree.key\n", ""
                ))

    def test_malformed_status_counts_are_explicit_errors(self):
        for record in ("# branch.ab +1", "# branch.ab a2 -1", "# branch.ab +² -1"):
            with self.subTest(record=record):
                result = launcher.inspect_repository(
                    Path("example"),
                    lambda c: subprocess.CompletedProcess(c, 0, record if "status" in c else "", ""),
                )
                self.assertEqual(result.error, "Invalid Git status counts")

    def test_enumeration_counts_plain_entries_and_repositories(self):
        for name in ("one", "two", "three"):
            (self.root / name / ".git").mkdir(parents=True)
        with patch.object(launcher, "MAX_ENTRIES", 2):
            with self.assertRaisesRegex(launcher.ProjectOperationError, "direct entries"):
                launcher.discover_repositories(self.root)
        with patch.object(launcher, "MAX_REPOSITORIES", 2):
            with self.assertRaisesRegex(launcher.ProjectOperationError, "repositories"):
                launcher.discover_repositories(self.root)

    def test_json_and_settings_have_preparse_budgets(self):
        with patch.object(launcher, "MAX_RESULT", 20):
            with self.assertRaisesRegex(launcher.ProjectOperationError, "result exceeded"):
                launcher.bounded_json({"message": "x" * 21})
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.base)}):
            path = launcher.settings_path()
            path.parent.mkdir()
            path.write_bytes(b" " * 65)
            with patch.object(launcher, "MAX_SETTINGS", 64):
                with self.assertRaisesRegex(launcher.ProjectOperationError, "16 KiB"):
                    launcher.load_settings()
            path.unlink()
            os.mkfifo(path)
            with self.assertRaisesRegex(launcher.ProjectOperationError, "regular file"):
                launcher.load_settings()

    def test_oversized_serialized_settings_do_not_replace_saved_choice(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.base)}):
            with patch.object(launcher.shutil, "which", return_value="/usr/bin/tool"):
                launcher.save_settings(launcher.LauncherSettings("terminal"))
                with self.assertRaisesRegex(launcher.ProjectOperationError, "16 KiB"):
                    launcher.save_settings(launcher.LauncherSettings("custom", "tool " + "\U0001f600" * 2000))
            self.assertEqual(launcher.load_settings().launcher, "terminal")

    def test_scan_deadline_does_not_print_partial_json(self):
        source = self.repo()
        source.rename(self.root / "source")
        with patch.object(launcher, "SCAN_SECONDS", 0), patch("sys.stdout", new_callable=io.StringIO) as out:
            with patch("sys.stderr", new_callable=io.StringIO) as err:
                self.assertEqual(launcher.main(["--root", str(self.root), "--json"]), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("time limit", err.getvalue())

    def test_import_preflight_rejects_bytes_entries_depth_and_special_files(self):
        source = self.repo()
        (source / "data").write_bytes(b"small")
        for method in ("copy", "move", "symlink"):
            for key, limit in (("IMPORT_BYTES", 1), ("IMPORT_ENTRIES", 1), ("IMPORT_DEPTH", 0)):
                with self.subTest(method=method, key=key), patch.object(launcher, key, limit):
                    with self.assertRaises(launcher.ProjectOperationError):
                        launcher.import_project(self.root, source, method)
                    self.assertTrue(source.exists())
                    self.assertEqual(list(self.root.iterdir()), [])
        os.mkfifo(source / "pipe")
        with self.assertRaisesRegex(launcher.ProjectOperationError, "special file"):
            launcher.import_project(self.root, source, "copy")

    def test_copy_enforces_budget_during_writes_and_cleans_only_staging(self):
        source = self.base / "source"
        source.mkdir()
        (source / "data").write_bytes(b"original")
        unrelated = self.root / "keep"
        unrelated.write_text("unchanged")
        actual_open = os.open

        def grow_file(path, flags, *args, **kwargs):
            if path == "data":
                (source / "data").write_bytes(b"original-more")
            return actual_open(path, flags, *args, **kwargs)

        with self.assertRaisesRegex(launcher.ProjectOperationError, "grew"):
            with launcher.staging_directory(self.root) as stage:
                with patch.object(os, "open", side_effect=grow_file):
                    launcher.inventory_tree(source, time.monotonic() + 2, stage)
        self.assertEqual(list(self.root.iterdir()), [unrelated])
        self.assertEqual(unrelated.read_text(), "unchanged")
        self.assertTrue(source.exists())

    def test_copy_preserves_restrictive_modes_and_extended_attributes(self):
        source = self.repo()
        source.chmod(0o700)
        data = source / "data"
        data.write_text("data")
        data.chmod(0o640)
        try:
            os.setxattr(data, "user.launcher-test", b"metadata")
        except OSError as error:
            if error.errno in (errno.ENOTSUP, errno.EPERM):
                self.skipTest("Test filesystem does not support user attributes")
            raise
        destination = launcher.import_project(self.root, source, "copy")
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
        self.assertEqual((destination / "data").stat().st_mode & 0o777, 0o640)
        self.assertEqual(os.getxattr(destination / "data", "user.launcher-test"), b"metadata")

    def test_cleanup_never_follows_staging_symlinks(self):
        unrelated = self.base / "keep"
        unrelated.mkdir()
        (unrelated / "data").write_text("keep")
        with launcher.staging_directory(self.root) as stage:
            (stage / "link").symlink_to(unrelated, target_is_directory=True)
        self.assertEqual((unrelated / "data").read_text(), "keep")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cross_device_move_does_not_fall_back_to_copy(self):
        source = self.repo()
        with patch.object(launcher, "publish_project", side_effect=OSError(errno.EXDEV, "cross-device")):
            with self.assertRaises(OSError):
                launcher.import_project(self.root, source, "move")
        self.assertTrue(source.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_publish_never_replaces_existing_empty_directory(self):
        source = self.base / "stage"
        source.mkdir()
        destination = self.root / "existing"
        destination.mkdir()
        with self.assertRaises(FileExistsError):
            launcher.publish_project(source, destination)
        self.assertTrue(source.exists())
        self.assertTrue(destination.exists())

    def test_local_clone_under_limits_checks_out_files(self):
        source = self.repo()
        (source / "tracked").write_text("hello")
        launcher.run_command(["git", "-C", str(source), "add", "tracked"])
        launcher.run_command(["git", "-C", str(source), "-c", "user.name=Test",
                              "-c", "user.email=test@example.invalid", "commit", "-qm", "initial"])
        destination = launcher.clone_project(self.root, str(source))
        self.assertEqual((destination / "tracked").read_text(), "hello")
        self.assertEqual(list(self.root.iterdir()), [destination])
        self.assertIsNone(launcher.inspect_repository(destination).error)

    def test_empty_local_clone_remains_supported(self):
        source = self.repo()
        destination = launcher.clone_project(self.root, str(source))
        self.assertTrue((destination / ".git").is_dir())
        self.assertIsNone(launcher.inspect_repository(destination).error)

    def test_git_memory_limit_is_applied_only_to_git_children(self):
        fake_git = self.base / "git"
        fake_git.write_text(
            f"#!{sys.executable}\n"
            "import resource\n"
            "print(resource.getrlimit(resource.RLIMIT_AS)[0])\n"
        )
        fake_git.chmod(0o700)
        result = launcher.run_command([str(fake_git)])
        self.assertEqual(int(result.stdout), launcher.GIT_MEMORY_BYTES)
        result = launcher.run_command(
            [sys.executable, "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_AS)[0])"]
        )
        import resource
        self.assertEqual(int(result.stdout), resource.getrlimit(resource.RLIMIT_AS)[0])

    def test_clone_entry_limit_and_deadline_are_live(self):
        fake_git = self.base / "git"
        fake_git.write_text(
            f"#!{sys.executable}\n"
            "import pathlib,sys,time\n"
            "stage=pathlib.Path(sys.argv[-1])\n"
            "for name in ('one','two','three'): (stage/name).touch()\n"
            "time.sleep(10)\n"
        )
        fake_git.chmod(0o700)
        with patch.dict(os.environ, {"PATH": f"{self.base}:{os.environ['PATH']}"}):
            for setting, value, message in (
                ("IMPORT_ENTRIES", 2, "entry limit"),
                ("IMPORT_SECONDS", 0.05, "time limit"),
            ):
                with self.subTest(setting=setting), patch.object(launcher, setting, value):
                    with self.assertRaisesRegex(launcher.ProjectOperationError, message):
                        launcher.clone_project(self.root, "https://example.invalid/repo.git")
                self.assertEqual(list(self.root.iterdir()), [])

    def test_clone_live_quota_failure_cleans_stage(self):
        fake_git = self.base / "git"
        fake_git.write_text(
            f"#!{sys.executable}\n"
            "import pathlib,sys,time\n"
            "stage=pathlib.Path(sys.argv[-1]); (stage/'growing').write_bytes(b'x'*65)\n"
            "time.sleep(10)\n"
        )
        fake_git.chmod(0o700)
        unrelated = self.root / "keep"
        unrelated.write_text("keep")
        with patch.dict(os.environ, {"PATH": f"{self.base}:{os.environ['PATH']}"}):
            with patch.object(launcher, "IMPORT_BYTES", 64):
                with self.assertRaisesRegex(launcher.ProjectOperationError, "byte limit"):
                    launcher.clone_project(self.root, "https://example.invalid/repo.git")
        self.assertEqual(list(self.root.iterdir()), [unrelated])
        self.assertEqual(unrelated.read_text(), "keep")

    def test_kernel_clone_file_limit_is_enforced(self):
        target = self.base / "output"
        with self.assertRaises(subprocess.CalledProcessError):
            launcher.run_command(
                [sys.executable, "-c", f"with open({str(target)!r},'wb') as f: f.write(b'x'*65)"],
                file_limit=64,
            )
        self.assertLessEqual(target.stat().st_size, 64)

    def test_import_timeout_does_not_publish_or_remove_source(self):
        source = self.repo()
        with patch.object(launcher, "IMPORT_SECONDS", 0):
            with self.assertRaisesRegex(launcher.ProjectOperationError, "time limit"):
                launcher.import_project(self.root, source, "copy")
        self.assertTrue(source.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_supervisor_cancellation_and_abrupt_qml_death_clean_worker_groups(self):
        launcher._linux_prctl(36, 1)
        fake_git = self.base / "git"
        marker = self.base / "processes.json"
        fake_git.write_text(
            f"#!{sys.executable}\n"
            "import json,os,pathlib,subprocess,sys,time\n"
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)'])\n"
            "stage=pathlib.Path(sys.argv[-1]); (stage/'partial').write_text('partial')\n"
            f"pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getppid(),os.getpid(),child.pid]))\n"
            "time.sleep(10)\n"
        )
        fake_git.chmod(0o700)
        env = dict(os.environ, PATH=f"{self.base}:{os.environ['PATH']}")
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=sig):
                marker.unlink(missing_ok=True)
                broker = subprocess.Popen(
                    [sys.executable, str(ROOT / "src" / "omarchy_project_launcher.py"),
                     "--supervise", "--root", str(self.root), "--clone", "https://example.invalid/repo.git"],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 3
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(marker.exists(), "worker did not start")
                    worker, git, child = json.loads(marker.read_text())
                    broker.send_signal(sig)
                    broker.communicate(timeout=8)
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        try:
                            pid, _ = os.waitpid(worker, os.WNOHANG)
                        except ChildProcessError:
                            break
                        if pid:
                            break
                        time.sleep(0.01)
                    for pid in (worker, git, child):
                        with self.assertRaises(ProcessLookupError, msg=f"Leaked process {pid}"):
                            os.kill(pid, 0)
                    self.assertEqual(list(self.root.iterdir()), [])
                finally:
                    if broker.poll() is None:
                        broker.terminate()
                        broker.communicate(timeout=8)

    def test_qml_has_no_line_or_end_collectors_and_has_watchdogs(self):
        qml = (ROOT / "ProjectLauncher.qml").read_text()
        collector = (ROOT / "BoundedProcess.qml").read_text()
        self.assertNotIn("StdioCollector", qml + collector)
        self.assertNotIn("LineParser", qml + collector)
        self.assertEqual(collector.count('splitMarker: ""'), 2)
        self.assertIn("data.length > limit - previous.length", collector)
        self.assertIn('"--supervise"', collector)
        self.assertIn("bounded.child.signal(9)", collector)
        self.assertIn("child.signal(15)", collector)
        self.assertIn("watchdog.restart()", collector)

    @unittest.skipUnless(shutil.which("qs"), "Quickshell is not installed")
    def test_qml_streaming_component_at_runtime(self):
        component = (ROOT / "BoundedProcess.qml").read_text()
        (self.base / "BoundedProcess.qml").write_text(
            component.replace("524289", "64").replace("8192", "32")
        )
        shutil.copyfile(ROOT / "tests" / "qml" / "BoundedProcessHarness.qml", self.base / "shell.qml")
        fake = self.base / "helper"
        runtime = self.base / "runtime"
        runtime.mkdir(mode=0o700)
        for code, expected in (
            ("print('hello', end='')", "success"),
            ("import os; os.write(1, b'x'*65)", "Helper output exceeded its limit"),
            ("import os; os.write(2, b'x'*33)", "Helper output exceeded its limit"),
            ("import time; time.sleep(10)", "Project helper timed out"),
        ):
            with self.subTest(code=code):
                fake.write_text(f"#!{sys.executable}\n{code}\n")
                fake.chmod(0o700)
                env = dict(os.environ, QT_QPA_PLATFORM="offscreen", XDG_RUNTIME_DIR=str(runtime),
                           XDG_CACHE_HOME=str(self.base / "cache"), QS_TEST_HELPER=str(fake))
                result = subprocess.run(
                    ["qs", "--no-color", "-p", str(self.base / "shell.qml")],
                    env=env, capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("RESULT:" + expected, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
