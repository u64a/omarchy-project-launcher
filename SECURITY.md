# Security

## Reporting a vulnerability

Please report suspected vulnerabilities privately through GitHub's security advisory feature for this repository. Do not open a public issue for an unpatched vulnerability.

## Security model

Omarchy plugins run unsandboxed with the current user's permissions. Project Launcher reads directory entries and hardened Git status output from the configured projects folder, then opens the user's configured CLI or a plain terminal in the selected repository.

Launcher preferences are stored in the user's XDG configuration directory, not in repositories. Setup writes them only after the user presses Save. Custom commands are split into executable arguments without shell interpretation and must use a command on PATH or an absolute executable path. Like the built-in launchers, custom commands run with normal user permissions; only configure commands you trust.

At the user's request, it can clone or create repositories, import an existing repository by symlink/move/copy, and move a direct-child project to the desktop Trash. Removal validates that the selected path is directly under the configured projects folder, uses `gio trash` rather than permanent deletion, and requires an additional confirmation for changed or untracked files. Trashing an imported symlink removes only the link, not its external target. The plugin does not require elevated privileges.

## Untrusted repositories

A Git repository carries its own configuration, and several configuration keys make Git execute a command. Status scans therefore run with `core.fsmonitor`, `core.hooksPath`, and the pager disabled.

Those flags do not cover content filters: `git status` runs `filter.<name>.clean` to re-hash modified files, and an in-tree `.gitattributes` cannot be disabled from the command line. A repository is therefore inspected for local configuration before any other Git command runs against it. A repository defining a content filter is reported as untrusted and is never scanned, and importing a repository whose configuration would run commands is refused. Removing a project whose status cannot be verified requires an explicit confirmation, so an untrusted repository can still be cleaned up.
