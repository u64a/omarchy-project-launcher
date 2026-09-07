# Changelog

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
