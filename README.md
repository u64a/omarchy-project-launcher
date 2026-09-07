# Omarchy Project Launcher

An open-source Omarchy plugin for browsing Git repositories, viewing live status, and opening a new GitHub Copilot CLI session in the selected project.

It scans direct children of `~/Projects` each time it opens. No project registration or database is required.

## Install

Install from the GitHub repository with Omarchy's plugin manager:

```bash
omarchy plugin add https://github.com/u64a/omarchy-project-launcher.git --enable
```

The plugin can be summoned with:

```bash
omarchy-shell shell summon io.github.u64a.project-launcher '{}'
```

Remove it with:

```bash
omarchy plugin remove io.github.u64a.project-launcher
```

For a convenient Omarchy menu entry, add this to `~/.config/omarchy/extensions/omarchy-menu.jsonc`:

```jsonc
"projects": {
  "icon": "󰲋",
  "label": "Projects",
  "description": "Open a Copilot session for a Git repository",
  "action": "omarchy-shell shell summon io.github.u64a.project-launcher '{}'"
},
```

## Configuration

By default, direct children of `~/Projects` are scanned each time the overlay opens. Set `OMARCHY_PROJECTS_ROOT` in the Omarchy shell environment to use another folder.

The launcher deliberately does not maintain a project registry or cache: adding or removing a repository is reflected the next time it opens.

## Requirements

- Omarchy Quattro with plugin support
- Python 3.10 or newer
- Git
- `xdg-terminal-exec`
- GitHub Copilot CLI (`copilot`)

The plugin runs unsandboxed with normal user permissions, as all Omarchy plugins do. It reads directory entries and Git metadata and launches the configured terminal; it does not modify repositories or require elevated privileges. Review [SECURITY.md](SECURITY.md) for the security model and reporting process.

## Marketplace readiness

The repository contains the required manifest, QML entry point, license, documentation, tests, and CI workflow. Before publishing a release, run the Omarchy validator and QML linter on an Omarchy installation.

## Development

Run tests:

```bash
python -m unittest discover -s tests -v
```

Validate the plugin on an Omarchy installation:

```bash
omarchy plugin validate .
qmllint -I "$OMARCHY_PATH/shell" ProjectLauncher.qml
```

The `bin/omarchy-project-launcher --list` command is useful for checking discovery without opening a UI.

## License

MIT. See [LICENSE](LICENSE).
