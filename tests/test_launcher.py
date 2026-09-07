from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.omarchy_project_launcher import (
    Project,
    project_json,
    choose_project,
    discover_repositories,
    inspect_repository,
    launch_copilot,
)


class LauncherTests(unittest.TestCase):
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
