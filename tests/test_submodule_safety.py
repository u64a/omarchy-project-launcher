from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import omarchy_project_launcher as launcher


ROOT = Path(__file__).resolve().parents[1]


class SubmoduleSafetyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        environment = patch.dict(os.environ, {
            "PATH": os.environ["PATH"],
            "HOME": str(self.base),
            "XDG_CONFIG_HOME": str(self.base / "xdg"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "TMPDIR": str(self.base),
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.root = self.base / "Projects"
        self.root.mkdir()
        self.source = self.base / "source"
        self.initialize(self.source)
        self.nested = self.source / "nested"
        self.initialize(self.nested)
        self.oid = self.git(self.nested, "rev-parse", "HEAD").stdout.strip()
        self.marker = self.base / "filter-executed"
        script = self.base / "filter.py"
        script.write_text(
            "import sys\nfrom pathlib import Path\n"
            f"Path({str(self.marker)!r}).write_text('FILTER_EXECUTED')\n"
            "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
        )
        self.filter_command = shlex.join([sys.executable, str(script)])

    def git(self, path, *arguments, input=None):
        return subprocess.run(
            ["git", "-C", str(path), *arguments], check=True,
            capture_output=True, text=True, timeout=5, input=input,
        )

    def initialize(self, path):
        self.git(self.base, "init", "--quiet", "-b", "main", str(path))
        self.git(path, "config", "user.name", "Test")
        self.git(path, "config", "user.email", "test@example.invalid")
        (path / "tracked.txt").write_text("original\n")
        (path / ".gitattributes").write_text("tracked.txt filter=sentinel\n")
        self.git(path, "add", ".")
        self.git(path, "commit", "--quiet", "-m", "initial")

    def add_gitlink(self, path=None, stage=0):
        path = path or self.source
        if stage:
            self.git(path, "update-index", "--index-info",
                     input=f"160000 {self.oid} {stage}\tnested\n")
        else:
            self.git(path, "update-index", "--add", "--cacheinfo",
                     f"160000,{self.oid},nested")

    def install_filter(self, included=False):
        if included:
            config = self.base / "included.config"
            self.git(self.base, "config", "--file", str(config),
                     "filter.sentinel.clean", self.filter_command)
            self.git(self.nested, "config", "include.path", str(config))
        else:
            self.git(self.nested, "config", "filter.sentinel.clean", self.filter_command)
        tracked = self.nested / "tracked.txt"
        timestamp = tracked.stat().st_mtime + 2
        tracked.write_text("modified\n")
        os.utime(tracked, (timestamp, timestamp))

    def assert_refused(self):
        commands = []

        def runner(command):
            commands.append(command)
            self.assertNotIn("status", command, "gitlinks must be refused before status")
            self.assertTrue("config" in command or "ls-files" in command)
            self.assertIn("core.fsmonitor=false", command)
            self.assertIn("core.hooksPath=/dev/null", command)
            self.assertIn("--no-optional-locks", command)
            return launcher.run_command(command)

        self.assertEqual(launcher.executable_config_keys(self.source), [])
        inspected = launcher.inspect_repository(self.source, runner)
        self.assertIn("Submodules", inspected.error)
        self.assertNotIn("clean", inspected.status_text)
        self.assertEqual(["config" if "config" in c else "ls-files" for c in commands],
                         ["config", "ls-files"])
        for method in ("symlink", "copy", "move"):
            with self.subTest(method=method):
                with self.assertRaisesRegex(launcher.ProjectOperationError, "Submodules"):
                    launcher.import_project(self.root, self.source, method, runner)
                self.assertTrue(self.source.exists())
                self.assertEqual(list(self.root.iterdir()), [])
                self.assertFalse(self.marker.exists())

    def test_populated_direct_and_included_filters_are_never_entered(self):
        self.add_gitlink()
        self.git(self.source, "commit", "--quiet", "-m", "gitlink")
        original = (self.nested / ".git" / "config").read_text()
        for included in (False, True):
            with self.subTest(included=included):
                (self.nested / ".git" / "config").write_text(original)
                self.install_filter(included)
                self.assert_refused()
                # Positive control: ordinary superproject status executes this fixture.
                self.git(self.source, "status", "--porcelain=v2")
                self.assertTrue(self.marker.exists())
                self.marker.unlink()

    def test_unpopulated_gitlink_is_not_reported_clean(self):
        self.add_gitlink()
        self.git(self.source, "commit", "--quiet", "-m", "gitlink")
        shutil.rmtree(self.nested)
        self.assert_refused()

    def test_all_conflict_stages_are_refused(self):
        for stage in (1, 2, 3):
            with self.subTest(stage=stage):
                self.git(self.source, "update-index", "--force-remove", "nested")
                self.add_gitlink(stage=stage)
                self.assert_refused()

    def test_nested_config_and_gitdir_redirects_are_not_read(self):
        self.add_gitlink()
        gitdir = self.nested / ".git"
        external = self.base / "nested.git"
        gitdir.rename(external)
        for content in ("not a gitfile\n", "gitdir: missing\n", f"gitdir: {external}\n"):
            with self.subTest(content=content):
                gitdir.write_text(content)
                self.assert_refused()
                gitdir.unlink()
        gitdir.symlink_to(external, target_is_directory=True)
        self.assert_refused()
        gitdir.unlink()
        external.rename(gitdir)
        (gitdir / "config").write_text("[malformed\n")
        self.assert_refused()

    def test_ignore_and_recursion_config_cannot_hide_gitlinks(self):
        self.add_gitlink()
        self.git(self.source, "config", "submodule.nested.ignore", "all")
        self.git(self.source, "config", "submodule.recurse", "false")
        self.git(self.source, "config", "diff.ignoreSubmodules", "all")
        self.assert_refused()

    def test_copy_rechecks_populated_and_unpopulated_gitlinks(self):
        self.install_filter(included=True)
        original_inventory = launcher.inventory_tree
        for populated in (True, False):
            def inventory(source, deadline, destination=None):
                result = original_inventory(source, deadline, destination)
                if destination is not None:
                    self.add_gitlink(destination)
                    if not populated:
                        shutil.rmtree(destination / "nested")
                return result

            with self.subTest(populated=populated), patch.object(
                launcher, "inventory_tree", inventory
            ):
                with self.assertRaisesRegex(launcher.ProjectOperationError, "Submodules"):
                    launcher.import_project(self.root, self.source, "copy")
            self.assertEqual(list(self.root.iterdir()), [])
            self.assertFalse(self.marker.exists())

    def test_status_defense_in_depth_does_not_enter_racing_gitlink(self):
        self.install_filter(included=True)
        self.git(self.source, "config", "submodule.recurse", "true")
        commands = []

        def runner(command):
            commands.append(command)
            if "status" in command:
                self.assertIn("--ignore-submodules=all", command)
                self.add_gitlink()
            return launcher.run_command(command)

        inspected = launcher.inspect_repository(self.source, runner)
        self.assertIsNone(inspected.error)
        self.assertEqual(sum("status" in command for command in commands), 1)
        self.assertFalse(self.marker.exists())
        self.assertIn("Submodules", launcher.inspect_repository(self.source).error)

    def test_untracked_nested_repositories_do_not_execute_config(self):
        original = (self.nested / ".git" / "config").read_text()
        for included in (False, True):
            with self.subTest(included=included):
                (self.nested / ".git" / "config").write_text(original)
                self.install_filter(included)
                self.git(self.nested, "config", "core.fsmonitor", self.filter_command)
                self.git(self.source, "config", "submodule.recurse", "true")
                inspected = launcher.inspect_repository(self.source)
                self.assertIsNone(inspected.error)
                self.assertEqual(inspected.untracked, 1)
                destination = launcher.import_project(self.root, self.source)
                self.assertIsNone(launcher.inspect_repository(destination).error)
                destination.unlink()
                self.assertFalse(self.marker.exists())

    def test_untracked_nested_gitdir_redirects_do_not_execute_filters(self):
        self.install_filter(included=True)
        gitdir = self.nested / ".git"
        external = self.base / "nested.git"
        gitdir.rename(external)
        for content in (f"gitdir: {external}\n", "gitdir: missing\n", "malformed\n"):
            with self.subTest(content=content):
                gitdir.write_text(content)
                inspected = launcher.inspect_repository(self.source)
                self.assertTrue(inspected.error or inspected.untracked)
                self.assertFalse(self.marker.exists())
                gitdir.unlink()

    def test_unverified_gitlinks_require_extra_trash_confirmation(self):
        self.add_gitlink()
        self.install_filter()
        project = self.root / "linked"
        project.symlink_to(self.source, target_is_directory=True)
        trashed = []

        def runner(command):
            self.assertNotIn("status", command)
            if command[0] == launcher.GIO_EXECUTABLE:
                trashed.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")
            return launcher.run_command(command)

        with self.assertRaisesRegex(launcher.ProjectOperationError, "confirm removal"):
            launcher.trash_project(self.root, project, runner=runner)
        self.assertEqual(trashed, [])
        launcher.trash_project(self.root, project, allow_dirty=True, runner=runner)
        self.assertEqual(trashed, [[launcher.GIO_EXECUTABLE, "trash", "--", str(project)]])
        self.assertTrue(self.source.exists())
        self.assertFalse(self.marker.exists())

    def test_clone_checks_head_gitlinks_before_checkout(self):
        self.add_gitlink()
        self.git(self.source, "commit", "--quiet", "-m", "gitlink")
        self.install_filter(included=True)
        commands = []

        def runner(command):
            commands.append(command)
            self.assertNotIn("checkout", command)
            return launcher.run_command(command)

        with self.assertRaisesRegex(launcher.ProjectOperationError, "Submodules"):
            launcher.clone_project(self.root, str(self.source), runner)
        self.assertTrue(any("ls-tree" in command for command in commands))
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse(self.marker.exists())

    def test_gitlink_inspection_failures_refuse_scan_and_import(self):
        record = f"100644 {self.oid} 0\ttracked.txt\0"
        cases = [
            ("MAX_RECORDS", 1, record * 2, "record limit"),
            ("MAX_LINE", 20, record, "record length limit"),
            ("MAX_STDOUT", 20, record, "byte limit"),
            ("MAX_RECORDS", 10, record[:-1], "Incomplete"),
            ("MAX_RECORDS", 10, "invalid\0", "Invalid"),
            ("MAX_RECORDS", 10, f"160000 {self.oid} 4\tnested\0", "Invalid"),
        ]
        for constant, limit, output, message in cases:
            def runner(command):
                self.assertNotIn("status", command)
                return subprocess.CompletedProcess(
                    command, 0, output if "ls-files" in command else "", ""
                )

            with self.subTest(message=message), patch.object(launcher, constant, limit):
                inspected = launcher.inspect_repository(self.source, runner)
                self.assertIn(message, inspected.error)
                self.assertNotIn("clean", inspected.status_text)
                with self.assertRaisesRegex(launcher.ProjectOperationError, message):
                    launcher.import_project(self.root, self.source, runner=runner)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_gitlink_command_never_falls_through_to_status_or_import(self):
        for failure in (
            subprocess.CalledProcessError(128, ["git"], stderr="invalid index"),
            launcher.ProjectOperationError("Operation exceeded its time limit"),
            OSError("cannot read index"),
        ):
            def runner(command):
                self.assertNotIn("status", command)
                if "ls-files" in command:
                    raise failure
                return subprocess.CompletedProcess(command, 0, "", "")

            with self.subTest(failure=failure):
                inspected = launcher.inspect_repository(self.source, runner)
                self.assertIsNotNone(inspected.error)
                self.assertNotIn("clean", inspected.status_text)
                with self.assertRaises(type(failure)):
                    launcher.import_project(self.root, self.source, runner=runner)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_nul_records_preserve_newlines_and_tabs_in_normal_filenames(self):
        (self.source / "strange\nname\t.txt").write_text("normal file\n")
        self.git(self.source, "add", "--", "strange\nname\t.txt")
        inspected = launcher.inspect_repository(self.source)
        self.assertIsNone(inspected.error)
        self.assertEqual(inspected.changed, 1)


if __name__ == "__main__":
    unittest.main()
