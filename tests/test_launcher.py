from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.omarchy_project_launcher import (
    Project,
    ProjectOperationError,
    choose_project,
    clone_project,
    create_project,
    discover_repositories,
    executable_config_keys,
    import_project,
    inspect_repository,
    launch_copilot,
    managed_project_path,
    project_json,
    run_command,
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
            return subprocess.CompletedProcess(command, 0, output, "")

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
            self.assertEqual(commands[0][-2:], ["https://github.com/acme/example.git", str(destination)])
            self.assertIn("core.hooksPath=/dev/null", commands[0])
            self.assertIn("--no-recurse-submodules", commands[0])

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

    @patch("src.omarchy_project_launcher.subprocess.Popen")
    def test_launches_copilot_in_project_directory(self, popen) -> None:
        project = Project("alpha", Path("/tmp/alpha"), "main")

        launch_copilot(project)

        popen.assert_called_once_with(
            [
                "xdg-terminal-exec",
                "--dir=/tmp/alpha",
                "copilot",
                "-C",
                "/tmp/alpha",
            ],
            start_new_session=True,
        )


if __name__ == "__main__":
    unittest.main()
