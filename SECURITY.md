# Security

## Reporting a vulnerability

Please report suspected vulnerabilities privately through GitHub's security advisory feature for this repository. Do not open a public issue for an unpatched vulnerability.

## Security model

Omarchy plugins run unsandboxed with the current user's permissions. Project Launcher reads directory entries and Git metadata from the configured projects folder, then starts the selected repository in the user's terminal with GitHub Copilot CLI. It does not modify repository files or require elevated privileges.
