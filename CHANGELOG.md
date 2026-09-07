# Changelog

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
