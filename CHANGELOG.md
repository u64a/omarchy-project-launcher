# Changelog

## 0.2.1

- Refuse to run status scans in a repository whose local Git config defines a content filter, closing an arbitrary command execution path triggered by opening the launcher.
- Reject imports of repositories whose Git config would run commands on the user's account.
- Require explicit confirmation before trashing a project whose status cannot be verified.

## 0.2.0

- Add projects by cloning an HTTPS or SSH Git URL.
- Create a new folder initialized as a Git repository on `main`.
- Import an existing Git repository by symlink, move, or copy, with symlink as the default.
- Move projects to the desktop Trash after confirmation.
- Require an additional explicit confirmation before trashing a repository with changed or untracked files.
- Validate all managed destinations and prevent removal outside the configured projects folder.
- Present import methods as a themed radio list instead of a native dropdown.
- Make every control keyboard reachable, with Tab focus, Escape to step back, and focus moved to the confirmation action.

## 0.1.4

- Add a privacy-safe marketplace preview image using fictional repositories.
- Update GitHub Actions to Node 24-compatible releases.
- Complete clean public-repository installation verification.

## 0.1.3

- Launch the selected AI terminal as a detached process before closing the overlay.
- Prevent the on-demand plugin lifecycle from terminating the launch process.

## 0.1.2

- Prevent repository-controlled filesystem monitor hooks from executing during project discovery.
- Disable Git hooks and optional index locking for read-only status scans.
- Add a regression test covering a malicious repository-local `core.fsmonitor` configuration.

## 0.1.1

- Fix marketplace-installed overlays failing to invoke the bundled launcher.
- Focus search automatically when the overlay opens.
- Add empty, loading, and error states.
- Add continuous integration and security documentation.

## 0.1.0

- Initial public release.
- Discover direct-child Git repositories under `~/Projects`.
- Display branch, working-tree, untracked, and ahead/behind status.
- Open a GitHub Copilot CLI session in the selected repository.
