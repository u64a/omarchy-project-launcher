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

## Requirements

- Omarchy Quattro with plugin support
- Python 3
- Git
- `xdg-terminal-exec`
- GitHub Copilot CLI (`copilot`)

The plugin runs with normal user permissions. It reads Git metadata and launches the configured terminal; it does not modify repositories.

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
