#!/usr/bin/env python3
"""Display Git repositories in the Omarchy menu and open a chosen launcher."""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import resource
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class ProjectOperationError(RuntimeError):
    """Raised when a requested project operation is unsafe or invalid."""


class OperationCancelled(BaseException):
    """Cancellation must not be mistaken for an incomplete but usable scan."""


MAX_STDOUT = 1024 * 1024
MAX_STDERR = 64 * 1024
MAX_RESULT = 512 * 1024
MAX_ERROR = 4096
MAX_ENTRIES = 4096
MAX_REPOSITORIES = 256
MAX_RECORDS = 20000
MAX_LINE = 16384
MAX_SETTINGS = 16384
MAX_ARGUMENT = 4096
SCAN_SECONDS = 60
IMPORT_SECONDS = 300
IMPORT_BYTES = 1024 * 1024 * 1024
IMPORT_ENTRIES = 20000
IMPORT_DEPTH = 64
CLONE_FILE_BYTES = 128 * 1024 * 1024
GIT_MEMORY_BYTES = 512 * 1024 * 1024
POLL_SECONDS = 0.1
TERM_SECONDS = 0.3
REAP_SECONDS = 0.7


def error_detail(error: BaseException) -> str:
    detail = getattr(error, "stderr", None)
    if not detail:
        # CalledProcessError.__str__ includes the complete command.
        detail = f"Command failed (exit {error.returncode})" if isinstance(
            error, subprocess.CalledProcessError
        ) else str(error)
    return str(detail)[:MAX_ERROR].strip()


def check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise ProjectOperationError("Operation exceeded its time limit")


def _linux_prctl(option: int, value: int) -> None:
    if ctypes.CDLL(None, use_errno=True).prctl(option, value, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot enable process supervision")


def _child_setup(parent: int, file_limit: int | None, memory_limit: int | None) -> None:
    # The helper is single-threaded. A killed QML broker must still cause its
    # worker to unwind and clean up that worker's separate Git process groups.
    _linux_prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    if os.getppid() != parent:
        os.kill(os.getpid(), signal.SIGTERM)
    if file_limit is not None or memory_limit is not None:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if file_limit is not None:
        # Report EFBIG instead of generating a core dump when a quota is hit.
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    for kind, limit in ((resource.RLIMIT_FSIZE, file_limit), (resource.RLIMIT_AS, memory_limit)):
        if limit is not None:
            existing = resource.getrlimit(kind)
            cap = min([limit, *(value for value in existing if value != resource.RLIM_INFINITY)])
            resource.setrlimit(kind, (cap, cap))


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def _cleanup_group(process: subprocess.Popen, grace: float = TERM_SECONDS) -> None:
    _signal_group(process.pid, signal.SIGTERM)
    end = time.monotonic() + grace
    while time.monotonic() < end:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    _signal_group(process.pid, signal.SIGKILL)
    end = time.monotonic() + REAP_SECONDS
    while time.monotonic() < end:
        process.poll()
        try:
            child, _ = os.waitpid(-process.pid, os.WNOHANG)
        except ChildProcessError:
            if process.returncode is not None:
                return
        else:
            if child:
                continue
        time.sleep(0.01)
    raise ProjectOperationError("Child cleanup timed out")


def command_seconds(command: Sequence[str]) -> float:
    if command[0] == "omarchy-menu-select":
        return 120
    if "clone" in command or "checkout" in command:
        return IMPORT_SECONDS
    if "config" in command:
        return 5
    if "status" in command:
        return 10
    return 15


def run_command(
    command: Sequence[str],
    *,
    timeout: float | None = None,
    stdout_limit: int = MAX_STDOUT,
    stderr_limit: int = MAX_STDERR,
    monitor: Callable[[], None] | None = None,
    file_limit: int | None = None,
    cleanup_grace: float = TERM_SECONDS,
) -> subprocess.CompletedProcess[str]:
    _linux_prctl(36, 1)  # PR_SET_CHILD_SUBREAPER: reap orphaned pipe holders.
    deadline = time.monotonic() + (command_seconds(command) if timeout is None else timeout)
    parent = os.getpid()
    memory_limit = GIT_MEMORY_BYTES if Path(command[0]).name == "git" else None
    environment = None
    if tuple(command[:len(GIT_INSPECTION)]) == GIT_INSPECTION:
        # Keep this policy in the child environment, including Git's children.
        # The argv prefix survives injected runners forwarding a copied list.
        environment = dict(os.environ, GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="")
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, preexec_fn=lambda: _child_setup(parent, file_limit, memory_limit),
        env=environment,
    )
    buffers = [bytearray(), bytearray()]
    limits = [stdout_limit, stderr_limit]
    try:
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((process.stdout, process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            while selector.get_map() or process.poll() is None:
                check_deadline(deadline)
                if monitor is not None:
                    monitor()
                for key, _ in selector.select(min(POLL_SECONDS, max(0, deadline - time.monotonic()))):
                    index = key.data
                    # Read at most the remaining budget plus one byte, before
                    # decoding or allocating a potentially hostile full line.
                    chunk = os.read(key.fd, min(65536, limits[index] - len(buffers[index]) + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif len(buffers[index]) + len(chunk) > limits[index]:
                        raise ProjectOperationError(
                            f"Command {'stdout' if index == 0 else 'stderr'} exceeded byte limit"
                        )
                    else:
                        buffers[index].extend(chunk)
            if monitor is not None:
                monitor()
    finally:
        # Even a successful parent can leave a descendant holding pipes, or
        # running after closing them. Always supervise the whole group.
        try:
            _cleanup_group(process, cleanup_grace)
        finally:
            process.stdout.close()
            process.stderr.close()
    stdout, stderr = (data.decode("utf-8", errors="replace") for data in buffers)
    if process.returncode:
        raise subprocess.CalledProcessError(
            process.returncode, command[:1], output=stdout[:MAX_ERROR], stderr=stderr[:MAX_ERROR]
        )
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def bounded_lines(text: str) -> list[str]:
    if len(text) > MAX_STDOUT or len(text.encode("utf-8")) > MAX_STDOUT:
        raise ProjectOperationError("Git output exceeded byte limit")
    if text.count("\n") > MAX_RECORDS:
        raise ProjectOperationError("Git output exceeded record limit")
    lines = text.splitlines()
    if len(lines) > MAX_RECORDS or any(len(line) > MAX_LINE for line in lines):
        raise ProjectOperationError("Git output exceeded record or line limit")
    return lines


def bounded_json(value: object) -> str:
    chunks = []
    size = 0
    for chunk in json.JSONEncoder(ensure_ascii=True).iterencode(value):
        size += len(chunk)
        if size > MAX_RESULT:
            raise ProjectOperationError("Helper result exceeded 512 KiB limit")
        chunks.append(chunk)
    return "".join(chunks)


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


GIT_HARDENING = (
    "--no-pager",
    "--no-optional-locks",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
)

GIT_INSPECTION = ("git", *GIT_HARDENING, "-c", "protocol.allow=never")


def inspection_command(path: Path, *arguments: str) -> list[str]:
    """Mark local-only Git argv for run_command's per-child transport policy.

    protocol.allow alone is insufficient: protocol.<name>.allow can override it.
    The empty GIT_ALLOW_PROTOCOL whitelist denies every transport, including
    external helpers, even on Git versions that ignore GIT_NO_LAZY_FETCH.
    Runners retain the single argv argument contract and must forward this argv
    to run_command when executing it, rather than bypassing its safety budgets.
    """
    return [*GIT_INSPECTION, "-C", str(path), *arguments]


# Repository-local configuration keys that make Git execute a command. A
# repository carries its own config, so these turn an ordinary scan into
# arbitrary code execution by the repository's author.
#
# Content filters are not covered by the hardening flags above:
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
        "core.gitproxy",
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
IMPORT_REMOTE_COMMAND_SUFFIXES = (".uploadpack", ".receivepack", ".vcs")


def executable_config_keys(
    path: Path, runner: CommandRunner = run_command, strict: bool = False
) -> list[str]:
    """Return repository-local executable or configuration-indirection keys.

    Configuration is read with the same no-transport boundary as object reads,
    without running filters, hooks, or pagers. Pass
    ``strict`` to also reject keys that execute later rather than during a scan.
    Indirections are refused rather than trusting an incomplete local-key list.
    """
    try:
        git_dir = path / ".git"
        if git_dir.is_symlink() or (git_dir.exists() and not git_dir.is_dir()):
            raise ProjectOperationError("Git directory redirects (.git files/symlinks) are not supported")
        for name in ("commondir", "config.worktree"):
            candidate = git_dir / name
            if candidate.exists() or candidate.is_symlink():
                raise ProjectOperationError(f"Git configuration indirection ({name}) is not supported")
        if (git_dir / "config").is_symlink():
            raise ProjectOperationError("Git configuration symlinks are not supported")
        result = runner(inspection_command(
            path, "config", "--local", "--no-includes", "--list", "--name-only"
        ))
    except (OSError, subprocess.CalledProcessError, ProjectOperationError) as error:
        raise ProjectOperationError(f"Cannot verify Git config: {error_detail(error)}") from error

    found = set()
    for key in bounded_lines(result.stdout):
        name = key.strip().lower()
        if not name:
            continue
        if name.startswith(("include.", "includeif.")) or name == "extensions.worktreeconfig":
            found.add(name)
        elif name == "extensions.partialclone" or (
            name.startswith("remote.") and name.endswith((".promisor", ".partialclonefilter"))
        ):
            raise ProjectOperationError(
                "Partial/promisor repositories are unsupported; inspection never fetches missing "
                "objects. Use a complete clone without partial/promisor configuration."
            )
        elif name.endswith(SCAN_EXECUTABLE_SUFFIXES):
            found.add(name)
        elif strict and (
            name in IMPORT_EXECUTABLE_KEYS
            or name.endswith(IMPORT_EXECUTABLE_SUFFIXES)
            or name.startswith(IMPORT_EXECUTABLE_PREFIXES)
            or (name.startswith("remote.") and name.endswith(IMPORT_REMOTE_COMMAND_SUFFIXES))
        ):
            found.add(name)
    return sorted(found)


def require_no_submodules(
    path: Path, runner: CommandRunner = run_command, *, head_tree: bool = False
) -> None:
    """Reject gitlinks without entering their repositories or running filters.

    A no-checkout clone has no populated index, so check its HEAD tree before
    checkout instead. Index enumeration includes every conflict stage.
    """
    command = ["ls-tree", "-r", "-z", "HEAD"] if head_tree else ["ls-files", "--stage", "-z"]
    result = runner(inspection_command(path, *command))
    text = result.stdout
    if len(text) > MAX_STDOUT or len(text.encode("utf-8")) > MAX_STDOUT:
        raise ProjectOperationError("Git gitlink inspection exceeded byte limit")
    if text.count("\0") > MAX_RECORDS:
        raise ProjectOperationError("Git gitlink inspection exceeded record limit")
    if text and not text.endswith("\0"):
        raise ProjectOperationError("Incomplete Git gitlink inspection")
    for record in text.split("\0")[:-1]:
        if len(record) > MAX_LINE:
            raise ProjectOperationError("Git gitlink inspection exceeded record length limit")
        metadata, separator, filename = record.partition("\t")
        fields = metadata.split(" ")
        if len(fields) != 3 or not separator or not filename:
            raise ProjectOperationError("Invalid Git gitlink inspection record")
        mode, kind, oid = fields if head_tree else (fields[0], fields[2], fields[1])
        if (
            mode not in {"100644", "100755", "120000", "160000"}
            or kind not in ({"blob", "commit"} if head_tree else {"0", "1", "2", "3"})
            or len(oid) not in {40, 64}
            or any(character not in "0123456789abcdef" for character in oid)
        ):
            raise ProjectOperationError("Invalid Git gitlink inspection metadata")
        if mode == "160000":
            raise ProjectOperationError(
                "Submodules (Git gitlinks) are unsupported; cannot verify repository status"
            )


def discover_repositories(root: Path) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"Projects folder does not exist: {root}")

    repositories = []
    deadline = time.monotonic() + 5
    with os.scandir(root) as entries:
        for count, entry in enumerate(entries, 1):
            check_deadline(deadline)
            if count > MAX_ENTRIES:
                raise ProjectOperationError("Projects folder exceeds 4096 direct entries")
            child = Path(entry.path)
            if entry.is_dir() and (child / ".git").exists():
                if len(repositories) >= MAX_REPOSITORIES:
                    raise ProjectOperationError("Projects folder exceeds 256 repositories")
                repositories.append(child)
    return sorted(repositories, key=lambda path: path.name.casefold())


def inspect_repository(path: Path, runner: CommandRunner = run_command) -> Project:
    try:
        unsafe = executable_config_keys(path, runner)
    except (ProjectOperationError, OSError) as error:
        return Project(path.name, path, "unknown", error=error_detail(error))
    if unsafe:
        return Project(
            path.name,
            path,
            "unknown",
            error=f"untrusted Git config ({', '.join(unsafe)[:MAX_ERROR - 80]}); status not run",
        )

    try:
        require_no_submodules(path, runner)
        result = runner(inspection_command(
            path, "status", "--porcelain=v2", "--branch", "--ignore-submodules=all"
        ))
        lines = bounded_lines(result.stdout)
    except (OSError, subprocess.CalledProcessError, ProjectOperationError) as error:
        return Project(path.name, path, "unknown", error=error_detail(error))

    branch = "unknown"
    changed = untracked = ahead = behind = 0

    for line in lines:
        if line.startswith("# branch.head "):
            branch = line.removeprefix("# branch.head ")
            if branch == "(detached)":
                branch = "detached"
        elif line.startswith("# branch.ab "):
            fields = line.split()
            if len(fields) != 4 or not fields[2].startswith("+") or not fields[3].startswith("-") or any(
                len(field) > 11 or not field[1:].isascii() or not field[1:].isdigit() for field in fields[2:]
            ):
                return Project(path.name, path, "unknown", error="Invalid Git status counts")
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
        or len(normalized) > 255
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
    if not source or len(source) > MAX_ARGUMENT:
        raise ProjectOperationError("Enter a Git repository URL")
    tail = source.rsplit("/", 1)[-1]
    if ":" in tail and "://" not in source:
        tail = tail.rsplit(":", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return validate_project_name(tail)


@dataclass
class Inventory:
    entries: int = 0
    bytes: int = 0


def inventory_tree(
    source: Path, deadline: float, destination: Path | None = None
) -> Inventory:
    """Count without following symlinks; copy regular files in bounded chunks."""
    inventory = Inventory()

    def walk(source_fd: int, target: Path | None, depth: int) -> None:
        check_deadline(deadline)
        if depth > IMPORT_DEPTH:
            raise ProjectOperationError("Import exceeds directory depth limit (64)")
        with os.scandir(source_fd) as entries:
            for entry in entries:
                check_deadline(deadline)
                inventory.entries += 1
                if inventory.entries > IMPORT_ENTRIES:
                    raise ProjectOperationError("Import exceeds entry limit (20000)")
                metadata = entry.stat(follow_symlinks=False)
                output = target / entry.name if target is not None else None
                if stat.S_ISDIR(metadata.st_mode):
                    if depth >= IMPORT_DEPTH:
                        raise ProjectOperationError("Import exceeds directory depth limit (64)")
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=source_fd)
                    try:
                        if output is not None:
                            output.mkdir(mode=stat.S_IMODE(metadata.st_mode) | 0o700)
                        walk(child, output, depth + 1)
                        if output is not None:
                            shutil.copystat(f"/proc/self/fd/{child}", output)
                    finally:
                        os.close(child)
                elif stat.S_ISLNK(metadata.st_mode):
                    if output is not None:
                        output.symlink_to(os.readlink(entry.name, dir_fd=source_fd))
                elif stat.S_ISREG(metadata.st_mode):
                    inventory.bytes += metadata.st_size
                    if inventory.bytes > IMPORT_BYTES:
                        raise ProjectOperationError("Import exceeds byte limit (1 GiB)")
                    if output is not None:
                        fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=source_fd)
                        with os.fdopen(fd, "rb") as reader, output.open("xb") as writer:
                            current = os.fstat(reader.fileno())
                            if not stat.S_ISREG(current.st_mode):
                                raise ProjectOperationError("Import source changed file type")
                            written = 0
                            while True:
                                check_deadline(deadline)
                                chunk = reader.read(min(65536, metadata.st_size - written + 1))
                                if not chunk:
                                    break
                                written += len(chunk)
                                if written > metadata.st_size:
                                    raise ProjectOperationError("Import source grew during copy")
                                writer.write(chunk)
                            if written != metadata.st_size:
                                raise ProjectOperationError("Import source changed during copy")
                            writer.flush()
                            shutil.copystat(f"/proc/self/fd/{reader.fileno()}", output)
                else:
                    raise ProjectOperationError("Import contains a special file (not a file, directory or link)")

    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(source_fd, destination, 0)
    finally:
        os.close(source_fd)
    return inventory


def remove_staging(path: Path) -> None:
    """Delete only an operation-owned tree, with fd-relative, no-follow walks."""
    deadline = time.monotonic() + 5

    def remove(directory: int, depth: int) -> None:
        if depth > IMPORT_DEPTH + 2:
            raise ProjectOperationError("Staging cleanup exceeded directory depth limit")
        os.fchmod(directory, stat.S_IMODE(os.fstat(directory).st_mode) | 0o700)
        with os.scandir(directory) as entries:
            for entry in entries:
                check_deadline(deadline)
                if entry.is_dir(follow_symlinks=False):
                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=directory)
                    try:
                        remove(child, depth + 1)
                    finally:
                        os.close(child)
                    os.rmdir(entry.name, dir_fd=directory)
                else:
                    os.unlink(entry.name, dir_fd=directory)

    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        remove(fd, 0)
    finally:
        os.close(fd)
    path.rmdir()


@contextmanager
def staging_directory(root: Path):
    stage = Path(tempfile.mkdtemp(prefix=".launcher-stage-", dir=root))
    try:
        yield stage
    finally:
        try:
            remove_staging(stage)
        except (OSError, ProjectOperationError, OperationCancelled) as error:
            raise ProjectOperationError(
                f"Could not finish staging cleanup; remove only {stage}: {error_detail(error)}"
            ) from error


def publish_project(source: Path, destination: Path) -> None:
    # Never replace even an empty folder created by somebody during the job.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        number = ctypes.get_errno()
        if number == errno.EXDEV:
            raise ProjectOperationError("Cross-filesystem moves are not supported; use Copy or Symlink")
        raise OSError(number, os.strerror(number), str(destination))


def clone_project(
    root: Path, url: str, runner: CommandRunner = run_command
) -> Path:
    source = url.strip()
    destination = destination_for(root, clone_name(source))
    deadline = time.monotonic() + IMPORT_SECONDS
    with staging_directory(root) as stage:
        checkout = stage / "project"
        checkout.mkdir()

        def monitor() -> None:
            try:
                inventory_tree(stage, deadline)
            except FileNotFoundError:
                # Git renames lockfiles while running. A final, quiescent scan
                # below is mandatory; a partial live scan is never trusted.
                check_deadline(deadline)

        def git(command: list[str]) -> None:
            if runner is run_command:
                run_command(command, timeout=max(0, deadline - time.monotonic()),
                            monitor=monitor, file_limit=CLONE_FILE_BYTES)
            else:
                runner(command)
                inventory_tree(stage, deadline)

        git([
            "git", *GIT_HARDENING, "clone", "--no-local", "--no-checkout",
            "--no-recurse-submodules", "--", source, str(checkout),
        ])
        unsafe = executable_config_keys(checkout, runner, strict=True)
        if unsafe:
            raise ProjectOperationError("Refusing checkout: untrusted or indirect cloned Git configuration")
        try:
            git(inspection_command(checkout, "rev-parse", "--verify", "--quiet", "HEAD"))
        except subprocess.CalledProcessError as error:
            if error.returncode != 1:
                raise
            # An empty repository has no HEAD commit to check out.
        else:
            require_no_submodules(checkout, runner, head_tree=True)
            git(inspection_command(checkout, "checkout", "--force", "--no-recurse-submodules"))
        require_no_submodules(checkout, runner)
        monitor()
        publish_project(checkout, destination)
    return destination


def create_project(
    root: Path, name: str, runner: CommandRunner = run_command
) -> Path:
    destination = destination_for(root, name)
    with staging_directory(root) as stage:
        checkout = stage / "project"
        checkout.mkdir()
        runner(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "init",
                "-b",
                "main",
                str(checkout),
            ]
        )
        publish_project(checkout, destination)
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

    deadline = time.monotonic() + IMPORT_SECONDS
    inventory_tree(source, deadline)
    unsafe = executable_config_keys(source, runner, strict=True)
    if unsafe:
        raise ProjectOperationError(
            "Refusing to import: this repository's Git config contains executable keys "
            f"or unsupported indirections ({', '.join(unsafe)[:MAX_ERROR // 2]}). "
            "Review its configuration in a trusted terminal."
        )
    require_no_submodules(source, runner)

    destination = destination_for(root, source.name)
    check_deadline(deadline)
    if method == "symlink":
        destination.symlink_to(source, target_is_directory=True)
    elif method == "move":
        publish_project(source, destination)
    elif method == "copy":
        with staging_directory(root) as stage:
            checkout = stage / "project"
            checkout.mkdir()
            inventory_tree(source, deadline, checkout)
            if executable_config_keys(checkout, runner, strict=True):
                raise ProjectOperationError("Copied Git config changed; refusing import")
            require_no_submodules(checkout, runner)
            shutil.copystat(source, checkout, follow_symlinks=False)
            check_deadline(deadline)
            publish_project(checkout, destination)
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

    if len(projects) > MAX_REPOSITORIES:
        raise ProjectOperationError("Menu exceeds repository limit")
    options = [menu_option(project) for project in projects]
    if sum(len(option.encode("utf-8")) for option in options) > MAX_RESULT:
        raise ProjectOperationError("Menu exceeds result byte limit")
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
    if "\0" in settings.custom_command or len(settings.custom_command) > MAX_ARGUMENT:
        raise ProjectOperationError("Custom command must be at most 4096 characters and contain no nulls")
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
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ProjectOperationError("Launcher settings must be a regular file")
            content = stream.read(MAX_SETTINGS + 1)
        if len(content) > MAX_SETTINGS:
            raise ProjectOperationError("Launcher settings exceed 16 KiB")
        data = json.loads(content)
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
    payload = json.dumps(
        {"launcher": settings.launcher, "custom_command": settings.custom_command}
    ) + "\n"
    if len(payload.encode("utf-8")) > MAX_SETTINGS:
        raise ProjectOperationError("Launcher settings exceed 16 KiB")
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
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
        print(bounded_json(settings_json()))
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
        if args.launch:
            selected = inspect_repository(args.launch.expanduser().resolve())
            if selected.error:
                raise ProjectOperationError(f"Cannot open {selected.name}: {selected.error}")
            launch_project(selected)
            return 0
        repositories = discover_repositories(root)
    except (FileNotFoundError, ProjectOperationError, OSError, subprocess.CalledProcessError) as error:
        print(error_detail(error), file=sys.stderr)
        return 1

    deadline = time.monotonic() + SCAN_SECONDS
    projects = []
    for path in repositories:
        check_deadline(deadline)
        projects.append(inspect_repository(path))

    if args.json:
        print(bounded_json([project_json(project) for project in projects]))
        return 0

    if args.list:
        text = "\n".join(f"{p.name}\t{p.status_text}\t{p.path}" for p in projects)
        if len(text.encode("utf-8")) > MAX_RESULT:
            raise ProjectOperationError("Helper result exceeded 512 KiB limit")
        print(text)
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


def helper_seconds(argv: Sequence[str]) -> int:
    if "--clone" in argv or "--import" in argv:
        return IMPORT_SECONDS
    if any(option in argv for option in ("--settings", "--set-launcher", "--launch", "--create", "--trash")):
        return 30
    if "--json" in argv or "--list" in argv:
        return SCAN_SECONDS
    return SCAN_SECONDS + 120


def _cancel(signum, frame) -> None:
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise OperationCancelled("Operation timed out" if signum == signal.SIGALRM else "Operation cancelled")


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)}
    try:
        for sig in previous:
            signal.signal(sig, _cancel)
        if len(argv) > 32 or any(len(arg) > MAX_ARGUMENT for arg in argv):
            raise ProjectOperationError("Too many or oversized helper arguments")
        if argv[:1] == ["--supervise"]:
            # QML owns only this broker. If it is killed during shell teardown,
            # PDEATHSIG lets the worker cancel its Git groups and staging work.
            result = run_command(
                [sys.executable, str(Path(__file__).resolve()), *argv[1:]],
                timeout=helper_seconds(argv[1:]) + 8,
                stdout_limit=MAX_RESULT + 1, stderr_limit=MAX_ERROR * 2,
                cleanup_grace=7,
            )
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            return 0
        signal.setitimer(signal.ITIMER_REAL, helper_seconds(argv))
        return _main(argv)
    except (ProjectOperationError, OSError, subprocess.CalledProcessError, OperationCancelled) as error:
        # ASCII makes chunked QML parsing independent of UTF-8 read boundaries.
        detail = error_detail(error).encode("ascii", errors="backslashreplace").decode("ascii")
        print(detail[:MAX_ERROR], file=sys.stderr)
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
