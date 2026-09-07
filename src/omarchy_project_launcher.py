#!/usr/bin/env python3
"""Display Git repositories in the Omarchy menu and launch Copilot."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class ProjectOperationError(RuntimeError):
    """Raised when a requested project operation is unsafe or invalid."""


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


def validate_project_name(name: str) -> str:
    normalized = name.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or normalized.startswith(".")
        or "/" in normalized
        or "\\" in normalized
        or "\0" in normalized
    ):
        raise ProjectOperationError("Project name must be a single visible folder name")
    return normalized


def destination_for(root: Path, name: str) -> Path:
    destination = root / validate_project_name(name)
    if destination.exists() or destination.is_symlink():
        raise ProjectOperationError(f"A project named '{destination.name}' already exists")
    return destination


def clone_name(url: str) -> str:
    source = url.strip().rstrip("/")
    if not source:
        raise ProjectOperationError("Enter a Git repository URL")
    tail = source.rsplit("/", 1)[-1]
    if ":" in tail and "://" not in source:
        tail = tail.rsplit(":", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return validate_project_name(tail)


def clone_project(
    root: Path, url: str, runner: CommandRunner = run_command
) -> Path:
    source = url.strip()
    destination = destination_for(root, clone_name(source))
    runner(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "clone",
            "--no-recurse-submodules",
            "--",
            source,
            str(destination),
        ]
    )
    return destination


def create_project(
    root: Path, name: str, runner: CommandRunner = run_command
) -> Path:
    destination = destination_for(root, name)
    destination.mkdir()
    try:
        runner(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "init",
                "-b",
                "main",
                str(destination),
            ]
        )
    except Exception:
        destination.rmdir()
        raise
    return destination


def import_project(root: Path, source: Path, method: str = "symlink") -> Path:
    source = source.expanduser().resolve()
    if not source.is_dir() or not (source / ".git").exists():
        raise ProjectOperationError("The selected folder is not a Git repository")
    if source == root or source in root.parents:
        raise ProjectOperationError("The projects folder or its parent cannot be imported")

    destination = destination_for(root, source.name)
    if method == "symlink":
        destination.symlink_to(source, target_is_directory=True)
    elif method == "move":
        shutil.move(str(source), str(destination))
    elif method == "copy":
        shutil.copytree(source, destination, symlinks=True)
    else:
        raise ProjectOperationError(f"Unsupported import method: {method}")
    return destination


def managed_project_path(root: Path, requested: Path) -> Path:
    root = root.expanduser().resolve()
    candidate = Path(os.path.abspath(requested.expanduser()))
    if candidate.parent.resolve() != root:
        raise ProjectOperationError("Only direct children of the projects folder can be removed")
    if not candidate.is_dir() or not (candidate / ".git").exists():
        raise ProjectOperationError("The selected path is not a managed Git project")
    return candidate


def trash_project(
    root: Path,
    requested: Path,
    allow_dirty: bool = False,
    runner: CommandRunner = run_command,
) -> None:
    project_path = managed_project_path(root, requested)
    project = inspect_repository(project_path)
    if project.error:
        raise ProjectOperationError(f"Cannot verify project status: {project.error}")
    if not allow_dirty and (project.changed or project.untracked):
        raise ProjectOperationError("Project has uncommitted changes; confirm dirty removal")
    runner(["gio", "trash", "--", str(project_path)])


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
    parser.add_argument(
        "--clone",
        metavar="GIT_URL",
        help="clone a Git repository into the projects folder",
    )
    parser.add_argument(
        "--create",
        metavar="NAME",
        help="create and initialize a project in the projects folder",
    )
    parser.add_argument(
        "--import",
        dest="import_path",
        type=Path,
        metavar="FOLDER",
        help="import an existing Git repository",
    )
    parser.add_argument(
        "--import-method",
        choices=("symlink", "move", "copy"),
        default="symlink",
        help="how to import an existing repository (default: symlink)",
    )
    parser.add_argument(
        "--trash",
        type=Path,
        metavar="REPOSITORY",
        help="move a direct-child project to the desktop Trash",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="allow moving a project with uncommitted changes to Trash",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.expanduser().resolve()
    try:
        if not root.is_dir():
            raise ProjectOperationError(f"Projects folder does not exist: {root}")
        if args.clone:
            print(clone_project(root, args.clone))
            return 0
        if args.create:
            print(create_project(root, args.create))
            return 0
        if args.import_path:
            print(import_project(root, args.import_path, args.import_method))
            return 0
        if args.trash:
            trash_project(root, args.trash, args.allow_dirty)
            return 0
        repositories = discover_repositories(root)
    except (FileNotFoundError, ProjectOperationError, OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", None) or str(error)
        print(detail.strip(), file=sys.stderr)
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
