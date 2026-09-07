#!/usr/bin/env python3
"""Display Git repositories in the Omarchy menu and launch Copilot."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    branch: str
    changed: int = 0
    untracked: int = 0
    ahead: int = 0
    behind: int = 0
    error: str | None = None

    @property
    def status_text(self) -> str:
        if self.error:
            return f"Git unavailable: {self.error}"

        parts = [self.branch]
        if self.changed == 0 and self.untracked == 0:
            parts.append("clean")
        else:
            if self.changed:
                parts.append(f"{self.changed} changed")
            if self.untracked:
                parts.append(f"{self.untracked} untracked")
        if self.ahead:
            parts.append(f"↑{self.ahead}")
        if self.behind:
            parts.append(f"↓{self.behind}")
        return " • ".join(parts)


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)


def discover_repositories(root: Path) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"Projects folder does not exist: {root}")

    return sorted(
        (child for child in root.iterdir() if child.is_dir() and (child / ".git").exists()),
        key=lambda path: path.name.casefold(),
    )


def inspect_repository(path: Path, runner: CommandRunner = run_command) -> Project:
    try:
        result = runner(
            [
                "git",
                "--no-pager",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(path),
                "status",
                "--porcelain=v2",
                "--branch",
            ]
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", None) or str(error)
        return Project(path.name, path, "unknown", error=detail.strip())

    branch = "unknown"
    changed = untracked = ahead = behind = 0

    for line in result.stdout.splitlines():
        if line.startswith("# branch.head "):
            branch = line.removeprefix("# branch.head ")
            if branch == "(detached)":
                branch = "detached"
        elif line.startswith("# branch.ab "):
            fields = line.split()
            ahead = int(fields[2].removeprefix("+"))
            behind = abs(int(fields[3]))
        elif line.startswith(("1 ", "2 ", "u ")):
            changed += 1
        elif line.startswith("? "):
            untracked += 1

    return Project(path.name, path, branch, changed, untracked, ahead, behind)


def menu_option(project: Project) -> str:
    icon = "󰊢" if not project.error else ""
    return f"{icon}\t{project.name}\t{project.status_text}"


def choose_project(
    projects: Sequence[Project], runner: CommandRunner = run_command
) -> Project | None:
    if not projects:
        return None

    options = [menu_option(project) for project in projects]
    try:
        result = runner(
            [
                "omarchy-menu-select",
                "Projects",
                *options,
                "--",
                "--width",
                "650",
            ]
        )
    except subprocess.CalledProcessError:
        return None

    selected_name = result.stdout.rstrip("\n").split("\t", 1)[0]
    return next((project for project in projects if project.name == selected_name), None)


def project_json(project: Project) -> dict[str, object]:
    return {
        "name": project.name,
        "path": str(project.path),
        "status": project.status_text,
        "branch": project.branch,
        "changed": project.changed,
        "untracked": project.untracked,
        "ahead": project.ahead,
        "behind": project.behind,
        "error": project.error,
    }


def launch_copilot(project: Project) -> None:
    subprocess.Popen(
        [
            "xdg-terminal-exec",
            f"--dir={project.path}",
            "copilot",
            "-C",
            str(project.path),
        ],
        start_new_session=True,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("OMARCHY_PROJECTS_ROOT", "~/Projects")).expanduser(),
        help="folder whose direct children are scanned (default: ~/Projects)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print project statuses without opening the menu",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print project statuses as JSON without opening the menu",
    )
    parser.add_argument(
        "--launch",
        type=Path,
        metavar="REPOSITORY",
        help="launch Copilot in a repository without opening the menu",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        repositories = discover_repositories(args.root.expanduser().resolve())
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 1

    projects = [inspect_repository(path) for path in repositories]

    if args.json:
        print(json.dumps([project_json(project) for project in projects]))
        return 0

    if args.launch:
        selected = inspect_repository(args.launch.expanduser().resolve())
        if selected.error:
            print(f"Cannot open {selected.name}: {selected.error}", file=sys.stderr)
            return 1
        launch_copilot(selected)
        return 0

    if args.list:
        for project in projects:
            print(f"{project.name}\t{project.status_text}\t{project.path}")
        return 0

    if not projects:
        print(f"No Git repositories found directly under {args.root}", file=sys.stderr)
        return 1

    selected = choose_project(projects)
    if selected is None:
        return 0
    if selected.error:
        print(f"Cannot open {selected.name}: {selected.error}", file=sys.stderr)
        return 1

    launch_copilot(selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
