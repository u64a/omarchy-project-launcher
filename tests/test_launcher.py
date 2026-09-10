from __future__ import annotations

import os
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.omarchy_project_launcher import (
    Project,
    ProjectOperationError,
    LauncherSettings,
    choose_project,
    clone_project,
    create_project,
    discover_repositories,
    executable_config_keys,
    import_project,
    inspect_repository,
    launch_project,
    load_settings,
    main,
    managed_project_path,
    project_json,
    run_command,
    save_settings,
    settings_json,
    settings_path,
    trash_project,
)


class LauncherTests(unittest.TestCase):
    def initialize_repository(self, path: Path) -> None:
        subprocess.run(["git", "init", "--quiet", "-b", "main", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
        subprocess.run(
            ["git", "-C", str(path), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        (path / "tracked.txt").write_text("tracked\n")
        subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(path), "commit", "--quiet", "-m", "initial"],
            check=True,
        )

    def test_discovers_only_direct_git_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "alpha" / ".git").mkdir(parents=True)
            (root / "plain").mkdir()
            (root / "group" / "nested" / ".git").mkdir(parents=True)

            self.assertEqual(discover_repositories(root), [root / "alpha"])

    def test_parses_git_status(self) -> None:
        output = "\n".join(
            [
                "# branch.oid abc123",
                "# branch.head main",
                "# branch.upstream origin/main",
                "# branch.ab +2 -1",
                "1 .M N... 100644 100644 100644 abc abc tracked.txt",
                "? new.txt",
            ]
        )

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            self.assertIn("core.fsmonitor=false", command)
            self.assertIn("core.hooksPath=/dev/null", command)
            self.assertIn("--no-optional-locks", command)
            if "status" in command:
                self.assertIn("--ignore-submodules=all", command)
            return subprocess.CompletedProcess(command, 0, output if "status" in command else "", "")

        project = inspect_repository(Path("/tmp/example"), runner)

        self.assertEqual(project.branch, "main")
        self.assertEqual(project.changed, 1)
        self.assertEqual(project.untracked, 1)
        self.assertEqual(project.ahead, 2)
        self.assertEqual(project.behind, 1)
        self.assertEqual(project.status_text, "main • 1 changed • 1 untracked • ↑2 • ↓1")

    def test_repository_fsmonitor_is_not_executed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            marker = repo / "fsmonitor-executed"
            hook = repo / "fsmonitor.sh"
            clean_env = {"PATH": os.environ["PATH"], "HOME": str(repo)}

            subprocess.run(["git", "init", "--quiet", str(repo)], check=True, env=clean_env)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Test"],
                check=True,
                env=clean_env,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
                check=True,
                env=clean_env,
            )
            (repo / "tracked.txt").write_text("tracked\n")
            subprocess.run(
                ["git", "-C", str(repo), "add", "tracked.txt"],
                check=True,
                env=clean_env,
            )
            subprocess.run(
                ["git", "-C", str(repo), "commit", "--quiet", "-m", "initial"],
                check=True,
                env=clean_env,
            )
            hook.write_text(f'#!/bin/sh\nprintf executed > "{marker}"\nprintf "\\n"\n')
            hook.chmod(0o755)
            subprocess.run(
                ["git", "-C", str(repo), "config", "core.fsmonitor", str(hook)],
                check=True,
                env=clean_env,
            )
            subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain=v2"],
                check=True,
                capture_output=True,
                env=clean_env,
            )
            self.assertTrue(marker.exists(), "test repository did not execute its fsmonitor")
            marker.unlink()

            with patch.dict(os.environ, clean_env, clear=True):
                project = inspect_repository(repo)

            self.assertIsNone(project.error)
            self.assertFalse(marker.exists())

    def test_clone_uses_safe_destination_and_disables_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands: list[list[str]] = []

            def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")

            destination = clone_project(root, "https://github.com/acme/example.git", runner)

            self.assertEqual(destination, root / "example")
            self.assertEqual(commands[0][-2], "https://github.com/acme/example.git")
            self.assertEqual(Path(commands[0][-1]).parent.parent, root)
            self.assertTrue(Path(commands[0][-1]).parent.name.startswith(".launcher-stage-"))
            self.assertEqual(list(root.iterdir()), [destination])
            self.assertIn("core.hooksPath=/dev/null", commands[0])
            self.assertIn("--no-recurse-submodules", commands[0])
            self.assertIn("--no-checkout", commands[0])
            self.assertIn("--no-local", commands[0])
            checkout = next(command for command in commands if "checkout" in command)
            self.assertIn("--no-recurse-submodules", checkout)

    def test_create_initializes_main_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = create_project(root, "new-project")

            branch = subprocess.run(
                ["git", "-C", str(destination), "branch", "--show-current"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(branch, "main")

    def test_import_supports_symlink_copy_and_move(self) -> None:
        for method in ("symlink", "copy", "move"):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = base / "Projects"
                source = base / f"source-{method}"
                root.mkdir()
                source.mkdir()
                self.initialize_repository(source)

                destination = import_project(root, source, method)

                self.assertTrue((destination / ".git").exists())
                self.assertEqual(destination.is_symlink(), method == "symlink")
                if method == "move":
                    self.assertFalse(source.exists())
                else:
                    self.assertTrue(source.exists())

    def test_managed_project_path_rejects_paths_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "Projects"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            self.initialize_repository(outside)

            with self.assertRaises(ProjectOperationError):
                managed_project_path(root, outside)

    def test_trash_requires_dirty_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Projects"
            project = root / "dirty"
            project.mkdir(parents=True)
            self.initialize_repository(project)
            (project / "untracked.txt").write_text("dirty\n")
            commands: list[list[str]] = []

            def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
                if command[0] == "gio":
                    commands.append(command)
                    return subprocess.CompletedProcess(command, 0, "", "")
                return run_command(command)

            with self.assertRaises(ProjectOperationError):
                trash_project(root, project, runner=runner)
            self.assertEqual(commands, [])

            trash_project(root, project, allow_dirty=True, runner=runner)
            self.assertEqual(commands, [["gio", "trash", "--", str(project)]])

    def test_scan_refuses_repository_with_content_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Projects"
            project = root / "hostile"
            project.mkdir(parents=True)
            self.initialize_repository(project)
            subprocess.run(
                ["git", "-C", str(project), "config", "filter.pwn.clean", "false"],
                check=True,
            )

            self.assertEqual(executable_config_keys(project), ["filter.pwn.clean"])

            inspected = inspect_repository(project)
            self.assertIsNotNone(inspected.error)
            self.assertIn("untrusted Git config", inspected.error)

    def test_import_refuses_repository_with_executable_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "Projects"
            source = base / "hostile"
            root.mkdir()
            source.mkdir()
            self.initialize_repository(source)
            subprocess.run(
                ["git", "-C", str(source), "config", "filter.pwn.clean", "false"],
                check=True,
            )

            with self.assertRaises(ProjectOperationError):
                import_project(root, source, "symlink")
            self.assertEqual(list(root.iterdir()), [])

    def test_untrusted_repository_can_still_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Projects"
            project = root / "hostile"
            project.mkdir(parents=True)
            self.initialize_repository(project)
            subprocess.run(
                ["git", "-C", str(project), "config", "filter.pwn.clean", "false"],
                check=True,
            )
            commands: list[list[str]] = []

            def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
                if command[0] == "gio":
                    commands.append(command)
                    return subprocess.CompletedProcess(command, 0, "", "")
                return run_command(command)

            with self.assertRaises(ProjectOperationError):
                trash_project(root, project, runner=runner)

            trash_project(root, project, allow_dirty=True, runner=runner)
            self.assertEqual(commands, [["gio", "trash", "--", str(project)]])

    def test_menu_selection_maps_back_to_project(self) -> None:
        projects = [Project("alpha", Path("/tmp/alpha"), "main")]

        def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command[0], "omarchy-menu-select")
            return subprocess.CompletedProcess(command, 0, "alpha\tmain • clean\n", "")

        self.assertEqual(choose_project(projects, runner), projects[0])

    @patch("src.omarchy_project_launcher.load_settings", return_value=LauncherSettings())
    @patch("src.omarchy_project_launcher.shutil.which", return_value="/usr/bin/tool")
    @patch("src.omarchy_project_launcher.subprocess.Popen")
    def test_launches_copilot_in_project_directory(self, popen, which, settings) -> None:
        project = Project("alpha", Path("/tmp/alpha"), "main")

        launch_project(project)

        popen.assert_called_once_with(
            [
                "xdg-terminal-exec",
                "--dir=/tmp/alpha",
                "copilot",
                "-C",
                "/tmp/alpha",
            ],
            cwd=project.path,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)})
        env.start()
        self.addCleanup(env.stop)

    def test_default_does_not_create_settings(self) -> None:
        self.assertEqual(load_settings(), LauncherSettings())
        self.assertFalse(settings_path().exists())

    @patch("src.omarchy_project_launcher.shutil.which", return_value="/usr/bin/tool")
    def test_choices_persist_across_reads(self, which) -> None:
        for launcher in ("copilot", "claude", "codex", "terminal", "custom"):
            with self.subTest(launcher=launcher):
                settings = LauncherSettings(launcher, 'my-tool --label "two words"')
                save_settings(settings)
                self.assertEqual(load_settings(), settings)
                self.assertEqual(settings_path().stat().st_mode & 0o777, 0o600)
                self.assertEqual(list(settings_path().parent.iterdir()), [settings_path()])

    @patch("src.omarchy_project_launcher.shutil.which", return_value="/usr/bin/tool")
    @patch("src.omarchy_project_launcher.subprocess.Popen")
    def test_launch_commands_use_project_directory_without_shell(self, popen, which) -> None:
        project = Project("spaced project", Path("/tmp/spaced project"), "main")
        for launcher, custom, expected in [
            ("claude", "", ["claude"]),
            ("codex", "", ["codex"]),
            ("terminal", "", []),
            ("custom", 'my-tool --label "two words" "; echo nope" "$HOME"',
             ["my-tool", "--label", "two words", "; echo nope", "$HOME"]),
        ]:
            with self.subTest(launcher=launcher):
                save_settings(LauncherSettings(launcher, custom))
                launch_project(project)
                self.assertEqual(popen.call_args.args[0], [
                    "xdg-terminal-exec", "--dir=/tmp/spaced project", *expected,
                ])
                self.assertEqual(popen.call_args.kwargs["cwd"], project.path)
                self.assertTrue(popen.call_args.kwargs["start_new_session"])
                self.assertNotIn("shell", popen.call_args.kwargs)

    def test_invalid_settings_are_reported_and_can_be_replaced(self) -> None:
        settings_path().parent.mkdir()
        for data in ('{', '[]', '{"launcher": 1}', '{"launcher": "other"}',
                     '{"launcher":"custom", "custom_command":2}'):
            with self.subTest(data=data):
                settings_path().write_text(data)
                with self.assertRaises(ProjectOperationError):
                    load_settings()
                status = settings_json()
                self.assertTrue(status["error"])
                self.assertEqual(status["launcher"], "")
                self.assertEqual(len(status["options"]), 5)
        with patch("src.omarchy_project_launcher.shutil.which", return_value="/usr/bin/tool"):
            save_settings(LauncherSettings("terminal"))
        self.assertEqual(load_settings().launcher, "terminal")

    @patch("src.omarchy_project_launcher.shutil.which", return_value="/usr/bin/tool")
    def test_invalid_custom_commands_do_not_overwrite_preference(self, which) -> None:
        save_settings(LauncherSettings("terminal"))
        for command in ("", "  ", '""', '"unclosed', "./from-repo", "bin/from-repo", "tool\0"):
            with self.subTest(command=command):
                with self.assertRaises(ProjectOperationError):
                    save_settings(LauncherSettings("custom", command))
                self.assertEqual(load_settings().launcher, "terminal")

    @patch("src.omarchy_project_launcher.shutil.which", return_value=None)
    @patch("src.omarchy_project_launcher.subprocess.Popen")
    def test_missing_command_blocks_launch_and_save(self, popen, which) -> None:
        with self.assertRaisesRegex(ProjectOperationError, "Command not found"):
            save_settings(LauncherSettings("copilot"))
        with self.assertRaisesRegex(ProjectOperationError, "Command not found"):
            launch_project(Project("alpha", Path("/tmp/alpha"), "main"))
        self.assertFalse(settings_path().exists())
        popen.assert_not_called()

    def test_availability_detects_missing_ai_without_blocking_terminal(self) -> None:
        with patch("src.omarchy_project_launcher.shutil.which",
                   side_effect=lambda name: "/usr/bin/terminal" if name == "xdg-terminal-exec" else None):
            options = settings_json()["options"]
            self.assertEqual([option["available"] for option in options], [False, False, False, True, True])
            with self.assertRaisesRegex(ProjectOperationError, "copilot"):
                save_settings(LauncherSettings())
            save_settings(LauncherSettings("terminal"))

    @patch("src.omarchy_project_launcher.shutil.which", return_value="/usr/bin/tool")
    def test_settings_cli_does_not_require_projects_folder(self, which) -> None:
        missing = str(self.config / "missing")
        self.assertEqual(main(["--root", missing, "--set-launcher", "custom",
                               "--custom-command", 'tool "two words"']), 0)
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["--root", missing, "--settings"]), 0)
        self.assertEqual(json.loads(output.getvalue())["launcher"], "custom")

    def test_cli_surfaces_save_errors(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(main(["--set-launcher", "custom", "--custom-command", '"']), 1)
        self.assertIn("Invalid custom command", error.getvalue())

    @patch("src.omarchy_project_launcher.shutil.which", return_value="/usr/bin/tool")
    def test_failed_atomic_save_preserves_settings_and_cleans_temporary_file(self, which) -> None:
        save_settings(LauncherSettings("terminal"))
        with patch.object(Path, "replace", side_effect=OSError("write failed")):
            with self.assertRaisesRegex(OSError, "write failed"):
                save_settings(LauncherSettings("claude"))
        self.assertEqual(load_settings().launcher, "terminal")
        self.assertEqual(list(settings_path().parent.iterdir()), [settings_path()])

    def test_detached_terminal_outlives_helper_without_holding_output_pipes(self) -> None:
        project = self.config / "spaced project"
        project.mkdir()
        subprocess.run(["git", "init", "--quiet", str(project)], check=True)
        binary = self.config / "bin"
        binary.mkdir()
        terminal = binary / "xdg-terminal-exec"
        marker = self.config / "launched.json"
        terminal.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "time.sleep(0.2)\n"
            f"Path({str(marker)!r}).write_text(json.dumps({{"
            "'argv': sys.argv[1:], 'cwd': os.getcwd(), 'detached': os.getsid(0) == os.getpid()"
            "}))\n"
        )
        terminal.chmod(0o700)
        with patch.dict(os.environ, {"PATH": f"{binary}:{os.environ['PATH']}"}):
            save_settings(LauncherSettings("terminal"))
            helper = Path(__file__).resolve().parents[1] / "bin" / "omarchy-project-launcher"
            result = subprocess.run(
                [str(helper), "--supervise", "--root", str(self.config), "--launch", str(project)],
                capture_output=True, text=True, timeout=5,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists())
        launched = json.loads(marker.read_text())
        self.assertEqual(launched["cwd"], str(project))
        self.assertEqual(launched["argv"], [f"--dir={project}"])
        self.assertTrue(launched["detached"])


if __name__ == "__main__":
    unittest.main()
