from __future__ import annotations

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
            return subprocess.CompletedProcess(command, 0, output, "")

        project = inspect_repository(Path("/tmp/example"), runner)

        self.assertEqual(project.branch, "main")
        self.assertEqual(project.changed, 1)
        self.assertEqual(project.untracked, 1)
        self.assertEqual(project.ahead, 2)
        self.assertEqual(project.behind, 1)
        self.assertEqual(project.status_text, "main • 1 changed • 1 untracked • ↑2 • ↓1")

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
