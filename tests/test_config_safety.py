from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import omarchy_project_launcher as launcher


ROOT = Path(__file__).resolve().parents[1]


class ConfigSafetyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.root = self.base / "Projects"
        self.root.mkdir()
        environment = patch.dict(os.environ, {
            "PATH": os.environ["PATH"],
            "HOME": str(self.base),
            "XDG_CONFIG_HOME": str(self.base / "xdg"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "TMPDIR": str(self.base),
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.source = self.base / "source"
        self.git(self.base, "init", "--quiet", "-b", "main", str(self.source))
        self.git(self.source, "config", "user.name", "Test")
        self.git(self.source, "config", "user.email", "test@example.invalid")
        (self.source / "tracked.txt").write_text("original\n")
        (self.source / ".gitattributes").write_text("tracked.txt filter=pwn\n")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "--quiet", "-m", "initial")
        self.modify_tracked(self.source)
        self.marker = self.base / "filter-executed"
        script = self.base / "filter.py"
        script.write_text(
            "import sys\nfrom pathlib import Path\n"
            f"Path({str(self.marker)!r}).write_text('executed')\n"
            "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
        )
        self.included = self.base / "included.config"
        self.git(self.base, "config", "--file", str(self.included),
                 "filter.pwn.clean", shlex.join([sys.executable, str(script)]))

    def git(self, path, *arguments):
        return subprocess.run(
            ["git", "-C", str(path), *arguments], check=True,
            capture_output=True, text=True, timeout=5,
        )

    def modify_tracked(self, source):
        tracked = source / "tracked.txt"
        timestamp = tracked.stat().st_mtime + 2
        # Same-size, stat-dirty content forces status to rehash through filters.
        tracked.write_text("modified\n")
        os.utime(tracked, (timestamp, timestamp))

    def assert_refused(self, source=None):
        source = source or self.source
        commands = []

        def runner(command):
            commands.append(command)
            self.assertIn("config", command, "only config reads may precede refusal")
            self.assertIn("--no-includes", command)
            return launcher.run_command(command)

        inspected = launcher.inspect_repository(source, runner)
        self.assertIsNotNone(inspected.error)
        self.assertNotIn("clean", inspected.status_text)
        for method in ("symlink", "copy", "move"):
            with self.subTest(method=method):
                with self.assertRaises(launcher.ProjectOperationError):
                    launcher.import_project(self.root, source, method, runner)
                self.assertTrue(source.exists())
                self.assertEqual(list(self.root.iterdir()), [])
                self.assertFalse(self.marker.exists())
        return commands

    def test_included_clean_filter_never_executes(self):
        self.git(self.source, "config", "include.path", str(self.included))
        self.assertEqual(launcher.executable_config_keys(self.source), ["include.path"])
        self.assert_refused()
        # Prove the fixture actually exercises Git's status-time clean filter.
        self.git(self.source, "status", "--porcelain=v2")
        self.assertTrue(self.marker.exists())

    def test_conditional_includes_are_refused_even_when_not_active(self):
        for condition in ("gitdir:**/source/.git", "gitdir/i:**/SOURCE/.git",
                          "onbranch:main", "onbranch:other",
                          "hasconfig:remote.*.url:https://example.invalid/**"):
            with self.subTest(condition=condition):
                config = self.source / ".git" / "config"
                original = config.read_text()
                self.git(self.source, "config", f"includeIf.{condition}.path", str(self.included))
                self.assert_refused()
                config.write_text(original)

    def test_missing_invalid_and_recursive_includes_fail_closed(self):
        self.git(self.source, "config", "include.path", str(self.included))
        for content in (None, "[invalid\n", f"[include]\npath = {self.included}\n"):
            with self.subTest(content=content):
                if content is None:
                    self.included.unlink()
                else:
                    self.included.write_text(content)
                self.assert_refused()

    def test_case_insensitive_indirection_keys_are_refused(self):
        config = self.source / ".git" / "config"
        original = config.read_text()
        for extra in (
            f'[InClUdE]\nPaTh = {self.included}\n',
            f'[InClUdEiF "onbranch:main"]\nPaTh = {self.included}\n',
            "[ExTeNsIoNs]\nWoRkTrEeCoNfIg = true\n",
            "[extensions]\nworktreeConfig = false\n",
        ):
            with self.subTest(extra=extra):
                config.write_text(original + extra)
                self.assert_refused()

    def test_worktree_specific_config_is_refused(self):
        self.git(self.source, "config", "extensions.worktreeConfig", "true")
        (self.source / ".git" / "config.worktree").write_bytes(self.included.read_bytes())
        self.assert_refused()
        self.git(self.source, "status", "--porcelain=v2")
        self.assertTrue(self.marker.exists())

    def test_worktree_config_without_local_extension_is_also_refused(self):
        (self.source / ".git" / "config.worktree").write_bytes(self.included.read_bytes())
        self.assert_refused()
        self.git(self.base, "config", "--global", "extensions.worktreeConfig", "true")
        self.assert_refused()

    def test_linked_worktree_gitfile_is_refused(self):
        worktree = self.base / "linked"
        self.git(self.source, "worktree", "add", "--quiet", "-b", "linked", str(worktree))
        self.git(self.source, "config", "extensions.worktreeConfig", "true")
        worktree_gitdir = Path((worktree / ".git").read_text().strip().removeprefix("gitdir: "))
        (worktree_gitdir / "config.worktree").write_bytes(self.included.read_bytes())
        self.modify_tracked(worktree)
        self.assert_refused(worktree)
        self.git(worktree, "status", "--porcelain=v2")
        self.assertTrue(self.marker.exists())

    def test_gitdir_and_config_symlinks_and_commondir_are_refused(self):
        gitdir = self.source / ".git"
        external = self.base / "external.git"
        gitdir.rename(external)
        for redirect in ("gitfile", "symlink", "commondir"):
            with self.subTest(redirect=redirect):
                if redirect == "gitfile":
                    gitdir.write_text(f"gitdir: {external}\n")
                elif redirect == "symlink":
                    gitdir.symlink_to(external, target_is_directory=True)
                else:
                    gitdir.mkdir()
                    (gitdir / "commondir").write_text(str(external))
                self.assert_refused()
                if redirect == "commondir":
                    (gitdir / "commondir").unlink()
                    gitdir.rmdir()
                else:
                    gitdir.unlink()
        external.rename(gitdir)
        (gitdir / "config").rename(self.base / "external.config")
        (gitdir / "config").symlink_to(self.base / "external.config")
        self.assert_refused()

    def test_copy_rechecks_config_before_publication(self):
        original_inventory = launcher.inventory_tree
        for kind in ("include", "worktree"):
            def inventory(source, deadline, destination=None):
                result = original_inventory(source, deadline, destination)
                if destination is not None:
                    if kind == "include":
                        with (destination / ".git" / "config").open("a") as stream:
                            stream.write(f"[include]\npath = {self.included}\n")
                    else:
                        (destination / ".git" / "config.worktree").write_bytes(
                            self.included.read_bytes()
                        )
                return result

            with self.subTest(kind=kind), patch.object(launcher, "inventory_tree", inventory):
                with self.assertRaises(launcher.ProjectOperationError):
                    launcher.import_project(self.root, self.source, "copy")
            self.assertEqual(list(self.root.iterdir()), [])
            self.assertFalse(self.marker.exists())
            self.assertEqual(launcher.executable_config_keys(self.source), [])

    def test_ordinary_status_and_trusted_global_includes_are_unchanged(self):
        self.included.write_text("[status]\nshowUntrackedFiles = no\n")
        self.git(self.base, "config", "--global", "include.path", str(self.included))
        (self.source / "untracked.txt").write_text("untracked\n")
        self.assertEqual(launcher.executable_config_keys(self.source), [])
        inspected = launcher.inspect_repository(self.source)
        self.assertIsNone(inspected.error)
        self.assertEqual((inspected.branch, inspected.changed, inspected.untracked), ("main", 1, 0))
        (self.source / "tracked.txt").write_text("original\n")
        self.assertIn("clean", launcher.inspect_repository(self.source).status_text)
        destination = launcher.import_project(self.root, self.source)
        self.assertIsNone(launcher.inspect_repository(destination).error)


if __name__ == "__main__":
    unittest.main()
