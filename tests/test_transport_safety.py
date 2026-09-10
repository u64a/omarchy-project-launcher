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


class TransportSafetyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        environment = patch.dict(os.environ, {
            "PATH": f"{self.base}{os.pathsep}{os.environ['PATH']}",
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
        self.git(self.base, "init", "--quiet", "-b", "main", str(self.source))
        self.git(self.source, "config", "user.name", "Test")
        self.git(self.source, "config", "user.email", "test@example.invalid")
        (self.source / "tracked").write_text("tracked\n")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "--quiet", "-m", "initial")
        self.git(self.source, "config", "uploadpack.allowFilter", "true")
        self.tree = self.git(self.source, "rev-parse", "HEAD^{tree}").stdout.strip()
        self.partial = self.base / "partial"
        self.git(self.base, "clone", "--quiet", "--no-checkout", "--filter=tree:0",
                 "--", self.source.as_uri(), str(self.partial))
        packs = list((self.partial / ".git" / "objects" / "pack").glob("*.idx"))
        self.assertTrue(packs)
        self.assertTrue(list((self.partial / ".git" / "objects" / "pack").glob("*.promisor")))
        for pack in packs:
            self.assertNotIn(self.tree, self.git(self.partial, "verify-pack", "-v", str(pack)).stdout)
        self.marker = self.base / "transport-executed"
        self.script = self.base / "sentinel.py"
        self.script.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\n"
            f"Path({str(self.marker)!r}).write_text('executed')\n"
            "raise SystemExit(1)\n"
        )
        self.script.chmod(0o755)
        self.sentinel = shlex.join([sys.executable, str(self.script)])

    def git(self, path, *arguments, check=True, env=None):
        return subprocess.run(
            ["git", "-C", str(path), *arguments], check=check,
            capture_output=True, text=True, timeout=10, env=env,
        )

    def configure_transport(self, kind):
        if kind == "uploadpack":
            self.git(self.partial, "config", "remote.origin.uploadpack", self.sentinel)
            protocol = "file"
        elif kind in ("vcs", "helper-url"):
            (self.base / "git-remote-sentinel").symlink_to(self.script)
            if kind == "vcs":
                self.git(self.partial, "config", "remote.origin.vcs", "sentinel")
            else:
                self.git(self.partial, "config", "remote.origin.url", "sentinel::unused")
            protocol = "sentinel"
        else:
            self.git(self.partial, "config", "remote.origin.url", f"ext::{self.sentinel}")
            protocol = "ext"
        self.git(self.partial, "config", f"protocol.{protocol}.allow", "always")
        return protocol

    def assert_partial_refused(self, path):
        commands = []

        def runner(command):
            commands.append(command)
            self.assertIn("config", command, "refuse before any object or index reads")
            return launcher.run_command(list(command))

        inspected = launcher.inspect_repository(path, runner)
        self.assertIn("Partial/promisor", inspected.error)
        self.assertNotIn("clean", inspected.status_text)
        for method in ("symlink", "copy", "move"):
            with self.subTest(method=method):
                with self.assertRaisesRegex(launcher.ProjectOperationError, "Partial/promisor"):
                    launcher.import_project(self.root, path, method, runner)
                self.assertTrue(path.exists())
                self.assertEqual(list(self.root.iterdir()), [])
                self.assertFalse(self.marker.exists())
        self.assertEqual(len(commands), 4)

    def test_true_partial_missing_tree_uploadpack_refused_before_status_and_import(self):
        self.configure_transport("uploadpack")
        self.assert_partial_refused(self.partial)
        # Positive control: real status lazily fetches the absent tree and runs
        # remote.origin.uploadpack, even though status is a read-only operation.
        result = self.git(self.partial, "status", "--porcelain=v2", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.marker.exists())

    def test_partial_keys_refused_case_insensitively_even_when_false_or_complete(self):
        config = self.source / ".git" / "config"
        original = config.read_text()
        for extra in (
            '[ReMoTe "other.with.dots"]\nPrOmIsOr = false\n',
            '[remote "origin"]\npartialCloneFilter = blob:none\n',
            '[ExTeNsIoNs]\nPaRtIaLcLoNe = origin\n',
        ):
            with self.subTest(extra=extra):
                config.write_text(original + extra)
                self.assert_partial_refused(self.source)

    def test_all_inspection_commands_keep_local_only_environment_and_budgets(self):
        self.configure_transport("uploadpack")
        real_popen = subprocess.Popen
        commands = (
            ("config", "--local", "--no-includes", "--list", "--name-only"),
            ("ls-files", "--stage", "-z"),
            ("ls-tree", "-r", "-z", "HEAD"),
            ("rev-parse", "--verify", "--quiet", "HEAD"),
            ("status", "--porcelain=v2", "--ignore-submodules=all"),
        )
        observed = []

        def popen(command, **kwargs):
            observed.append(command)
            self.assertEqual(kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
            self.assertEqual(kwargs["env"]["GIT_ALLOW_PROTOCOL"], "")
            self.assertTrue(kwargs["start_new_session"])
            self.assertIsNotNone(kwargs["preexec_fn"])
            self.assertIn("protocol.allow=never", command)
            return real_popen(command, **kwargs)

        with patch.dict(os.environ, {"GIT_NO_LAZY_FETCH": "0", "GIT_ALLOW_PROTOCOL": "file"}):
            before = dict(os.environ)
            with patch.object(launcher.subprocess, "Popen", side_effect=popen):
                for arguments in commands:
                    with self.subTest(arguments=arguments):
                        command = launcher.inspection_command(self.partial, *arguments)
                        if arguments[0] in ("ls-tree", "status"):
                            with self.assertRaises(subprocess.CalledProcessError):
                                launcher.run_command(list(command))
                        else:
                            launcher.run_command(list(command))
                        self.assertFalse(self.marker.exists())
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(len(observed), len(commands))

    def test_transport_deny_blocks_lazy_fetch_even_without_git_no_lazy_fetch(self):
        original = (self.partial / ".git" / "config").read_text()
        real_popen = subprocess.Popen

        def old_git(command, **kwargs):
            self.assertEqual(kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
            self.assertEqual(kwargs["env"]["GIT_ALLOW_PROTOCOL"], "")
            # Simulate Git versions predating GIT_NO_LAZY_FETCH: the transport
            # whitelist must remain sufficient without that newer feature.
            kwargs["env"]["GIT_NO_LAZY_FETCH"] = "0"
            return real_popen(command, **kwargs)

        for kind in ("uploadpack", "vcs", "helper-url", "ext"):
            with self.subTest(kind=kind):
                (self.partial / ".git" / "config").write_text(original)
                helper = self.base / "git-remote-sentinel"
                if helper.is_symlink():
                    helper.unlink()
                protocol = self.configure_transport(kind)
                for operation in (("status", "--porcelain=v2"), ("ls-tree", "-r", "-z", "HEAD")):
                    with patch.object(launcher.subprocess, "Popen", side_effect=old_git):
                        with self.assertRaises(subprocess.CalledProcessError) as raised:
                            launcher.run_command(launcher.inspection_command(self.partial, *operation))
                    self.assertIn(f"transport '{protocol}' not allowed", raised.exception.stderr)
                    self.assertFalse(self.marker.exists())
                # A global deny alone is overridden by protocol.<name>.allow.
                result = self.git(self.partial, "-c", "protocol.allow=never",
                                  "status", "--porcelain=v2", check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(self.marker.exists(), f"{kind} fixture never ran its command")
                self.marker.unlink()

    def test_empty_allow_protocol_overrides_command_and_global_protocol_whitelists(self):
        self.git(self.base, "config", "--global", "protocol.file.allow", "always")
        with patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": ""}):
            result = self.git(self.source, "-c", "protocol.allow=always",
                              "-c", "protocol.file.allow=always", "ls-remote",
                              "--", self.source.as_uri(), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("transport 'file' not allowed", result.stderr)
        # Explicitly allowing file is a positive control for the very same URL.
        with patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}):
            self.assertIn("HEAD", self.git(self.source, "ls-remote", "--", self.source.as_uri()).stdout)

    def test_missing_tree_from_trusted_global_promisor_config_cannot_fetch(self):
        self.configure_transport("uploadpack")
        self.git(self.partial, "config", "--unset", "remote.origin.promisor")
        self.git(self.partial, "config", "--unset", "remote.origin.partialclonefilter")
        self.git(self.base, "config", "--global", "remote.origin.promisor", "true")
        self.assertEqual(launcher.executable_config_keys(self.partial), [])
        inspected = launcher.inspect_repository(self.partial)
        self.assertIsNotNone(inspected.error)
        self.assertNotIn("clean", inspected.status_text)
        self.assertFalse(self.marker.exists())

    def test_remote_command_fields_refused_by_import_but_ordinary_urls_supported(self):
        config = self.source / ".git" / "config"
        original = config.read_text()
        for key in ("remote.origin.uploadpack", "remote.origin.receivepack",
                    "remote.other.with.dots.vcs", "core.gitproxy"):
            with self.subTest(key=key):
                config.write_text(original)
                self.git(self.source, "config", key, self.sentinel)
                self.assertIn(key, launcher.executable_config_keys(self.source, strict=True))
                self.assertIsNone(launcher.inspect_repository(self.source).error)
                for method in ("symlink", "copy", "move"):
                    with self.assertRaises(launcher.ProjectOperationError):
                        launcher.import_project(self.root, self.source, method)
                self.assertEqual(list(self.root.iterdir()), [])
                self.assertFalse(self.marker.exists())
        config.write_text(original)
        self.git(self.source, "config", "remote.origin.url", "https://github.com/acme/example.git")
        self.assertEqual(launcher.executable_config_keys(self.source, strict=True), [])
        self.assertIsNone(launcher.inspect_repository(self.source).error)
        destination = launcher.import_project(self.root, self.source)
        self.assertIsNone(launcher.inspect_repository(destination).error)

    def test_copy_rechecks_partial_config_before_publication(self):
        inventory = launcher.inventory_tree

        def changed(source, deadline, destination=None):
            result = inventory(source, deadline, destination)
            if destination is not None:
                self.git(destination, "config", "remote.origin.promisor", "true")
            return result

        with patch.object(launcher, "inventory_tree", changed):
            with self.assertRaisesRegex(launcher.ProjectOperationError, "Partial/promisor"):
                launcher.import_project(self.root, self.source, "copy")
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertTrue(self.source.exists())

    def test_explicit_clone_allows_local_transport_but_post_clone_is_local_only(self):
        real_popen = subprocess.Popen
        seen = []

        def popen(command, **kwargs):
            seen.append(command)
            if "clone" in command:
                self.assertIsNone(kwargs["env"])
                self.assertNotIn("protocol.allow=never", command)
            else:
                self.assertEqual(kwargs["env"]["GIT_ALLOW_PROTOCOL"], "")
                self.assertEqual(kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
            return real_popen(command, **kwargs)

        with patch.object(launcher.subprocess, "Popen", side_effect=popen):
            destination = launcher.clone_project(self.root, self.source.as_uri())
        self.assertEqual((destination / "tracked").read_text(), "tracked\n")
        for operation in ("clone", "config", "rev-parse", "ls-tree", "checkout", "ls-files"):
            self.assertTrue(any(operation in command for command in seen), operation)
        self.assertIsNone(launcher.inspect_repository(destination).error)


if __name__ == "__main__":
    unittest.main()
