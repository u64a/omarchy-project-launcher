#!/usr/bin/env python3
"""Display Git repositories in the Omarchy menu and open a chosen launcher."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
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


GIT_HARDENING = (
    "--no-pager",
    "--no-optional-locks",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
)

# Repository-local configuration keys that make Git execute a command. A
# repository carries its own config, so these turn an ordinary scan into
# arbitrary code execution by the repository's author.
#
# Content filters are the only vector that survives the hardening flags above:
# `git status` runs filter.<name>.clean to re-hash stat-dirty files, and there
# is no flag that disables an in-tree .gitattributes. Keys such as
# core.fsmonitor, core.hooksPath, and core.pager are already neutralised by
# GIT_HARDENING, so they are deliberately not treated as untrusted here.
SCAN_EXECUTABLE_SUFFIXES = (".clean", ".smudge", ".process")

# Import is an explicit, one-time action, so it is vetted more strictly: these
# keys do not execute during a scan, but would run commands the next time the
# user works in the repository themselves.
IMPORT_EXECUTABLE_KEYS = frozenset(
    {
        "core.fsmonitor",
        "core.hookspath",
        "core.sshcommand",
        "core.pager",
        "core.editor",
        "core.askpass",
        "credential.helper",
        "sequence.editor",
        "gpg.program",
        "init.templatedir",
    }
)
IMPORT_EXECUTABLE_SUFFIXES = SCAN_EXECUTABLE_SUFFIXES + (".textconv", ".command")
IMPORT_EXECUTABLE_PREFIXES = ("alias.",)


def executable_config_keys(
    path: Path, runner: CommandRunner = run_command, strict: bool = False
) -> list[str]:
    """Return repository-local config keys that would execute a command.

    Reading configuration never runs filters, hooks, or pagers, so this is safe
    to call before any other Git command touches an untrusted repository. Pass
    ``strict`` to also reject keys that execute later rather than during a scan.
    """
    try:
        result = runner(
            ["git", *GIT_HARDENING, "-C", str(path), "config", "--local", "--list", "--name-only"]
        )
    except (OSError, subprocess.CalledProcessError):
        return []

    found = set()
    for key in result.stdout.splitlines():
        name = key.strip().lower()
        if not name:
            continue
        if name.endswith(SCAN_EXECUTABLE_SUFFIXES):
            found.add(name)
        elif strict and (
            name in IMPORT_EXECUTABLE_KEYS
            or name.endswith(IMPORT_EXECUTABLE_SUFFIXES)
            or name.startswith(IMPORT_EXECUTABLE_PREFIXES)
        ):
            found.add(name)
    return sorted(found)


def discover_repositories(root: Path) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"Projects folder does not exist: {root}")

    return sorted(
        (child for child in root.iterdir() if child.is_dir() and (child / ".git").exists()),
        key=lambda path: path.name.casefold(),
    )


def inspect_repository(path: Path, runner: CommandRunner = run_command) -> Project:
    unsafe = executable_config_keys(path, runner)
    if unsafe:
        return Project(
            path.name,
            path,
            "unknown",
            error=f"untrusted Git config ({', '.join(unsafe)}); status not run",
        )

    try:
        result = runner(
            [
                "git",
                *GIT_HARDENING,
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


def import_project(
    root: Path,
    source: Path,
    method: str = "symlink",
    runner: CommandRunner = run_command,
) -> Path:
    source = source.expanduser().resolve()
    if not source.is_dir() or not (source / ".git").exists():
        raise ProjectOperationError("The selected folder is not a Git repository")
    if source == root or source in root.parents:
        raise ProjectOperationError("The projects folder or its parent cannot be imported")

    unsafe = executable_config_keys(source, runner, strict=True)
    if unsafe:
        raise ProjectOperationError(
            "Refusing to import: this repository's Git config would run commands "
            f"on your account ({', '.join(unsafe)}). Remove these keys from "
            f"{source}/.git/config if you trust it."
        )

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
    project = inspect_repository(project_path, runner)
    if not allow_dirty:
        if project.error:
            raise ProjectOperationError(
                f"Cannot verify project status ({project.error}); confirm removal"
            )
        if project.changed or project.untracked:
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


LAUNCHERS = {
    "copilot": ("GitHub Copilot CLI", "copilot"),
    "claude": ("Claude Code", "claude"),
    "codex": ("Codex CLI", "codex"),
    "terminal": ("Plain terminal", ""),
    "custom": ("Custom command", ""),
}


@dataclass(frozen=True)
class LauncherSettings:
    launcher: str = "copilot"
    custom_command: str = ""


def settings_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "omarchy-project-launcher" / "settings.json"


def validate_settings(settings: LauncherSettings) -> list[str]:
    if settings.launcher not in LAUNCHERS:
        raise ProjectOperationError("Unknown launcher; choose one in Setup")
    if "\0" in settings.custom_command:
        raise ProjectOperationError("Custom command must not contain null characters")
    if settings.launcher != "custom":
        executable = LAUNCHERS[settings.launcher][1]
        return [executable] if executable else []
    try:
        command = shlex.split(settings.custom_command)
    except ValueError as error:
        raise ProjectOperationError(f"Invalid custom command: {error}") from error
    if not command or not command[0]:
        raise ProjectOperationError("Enter a custom command")
    command[0] = os.path.expanduser(command[0])
    if "/" in command[0] and not Path(command[0]).is_absolute():
        raise ProjectOperationError("Use an absolute executable path or a command on PATH")
    return command


def load_settings() -> LauncherSettings:
    path = settings_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return LauncherSettings()
    except (OSError, ValueError) as error:
        raise ProjectOperationError(f"Cannot read launcher settings: {error}") from error
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("launcher"), str)
        or not isinstance(data.get("custom_command", ""), str)
    ):
        raise ProjectOperationError("Invalid launcher settings; choose a launcher in Setup")
    settings = LauncherSettings(data["launcher"], data.get("custom_command", ""))
    validate_settings(settings)
    return settings


def require_launcher(settings: LauncherSettings) -> list[str]:
    command = validate_settings(settings)
    for executable in ["xdg-terminal-exec", *command[:1]]:
        if shutil.which(executable) is None:
            raise ProjectOperationError(
                f"Command not found: {executable}. Install it or choose another launcher in Setup."
            )
    return command


def save_settings(settings: LauncherSettings) -> None:
    require_launcher(settings)
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(
                {"launcher": settings.launcher, "custom_command": settings.custom_command},
                stream,
            )
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def settings_json() -> dict[str, object]:
    terminal_available = shutil.which("xdg-terminal-exec") is not None
    result: dict[str, object] = {
        "options": [
            {
                "id": key,
                "name": name,
                "available": terminal_available and (not executable or shutil.which(executable) is not None),
            }
            for key, (name, executable) in LAUNCHERS.items()
        ],
        "launcher": "",
        "custom_command": "",
        "error": "",
    }
    try:
        settings = load_settings()
    except ProjectOperationError as error:
        result["error"] = str(error)
    else:
        result.update(launcher=settings.launcher, custom_command=settings.custom_command)
    return result


def launch_project(project: Project) -> None:
    settings = load_settings()
    command = require_launcher(settings)
    if settings.launcher == "copilot":
        command.extend(["-C", str(project.path)])
    # The terminal must not keep the overlay helper's output collectors open.
    subprocess.Popen(
        ["xdg-terminal-exec", f"--dir={project.path}", *command],
        cwd=project.path,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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
        help="open the configured launcher in a repository without opening the menu",
    )
    parser.add_argument("--settings", action="store_true", help="show launcher settings and availability as JSON")
    parser.add_argument("--set-launcher", choices=LAUNCHERS, help="save the preferred launcher")
    parser.add_argument("--custom-command", default="", help="executable and arguments for the custom launcher")
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


def _main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.settings:
        print(json.dumps(settings_json()))
        return 0
    if args.set_launcher:
        save_settings(LauncherSettings(args.set_launcher, args.custom_command))
        return 0
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
        launch_project(selected)
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

    launch_project(selected)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _main(argv)
    except (ProjectOperationError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
