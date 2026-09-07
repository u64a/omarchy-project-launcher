# Security

## Reporting a vulnerability

Please report suspected vulnerabilities privately through GitHub's security advisory feature for this repository. Do not open a public issue for an unpatched vulnerability.

## Security model

Omarchy plugins run unsandboxed with the current user's permissions. Project Launcher reads directory entries and hardened Git status output from the configured projects folder, then starts the selected repository in the user's terminal with GitHub Copilot CLI.

At the user's request, it can clone or create repositories, import an existing repository by symlink/move/copy, and move a direct-child project to the desktop Trash. Removal validates that the selected path is directly under the configured projects folder, uses `gio trash` rather than permanent deletion, and requires an additional confirmation for changed or untracked files. Trashing an imported symlink removes only the link, not its external target. The plugin does not require elevated privileges.
