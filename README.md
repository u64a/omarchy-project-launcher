# Omarchy Project Launcher

![Project Launcher with project search, Git status, Add, Trash, and Setup controls](preview.png)

*Project-list preview uses fictional repositories.*

An open-source Omarchy plugin for browsing Git repositories, viewing live status, and opening your preferred CLI or terminal in the selected project.

It scans direct children of `~/Projects` each time it opens. No project registration or database is required.

## Features

- View branch, changed, untracked, and ahead/behind status.
- Choose GitHub Copilot CLI (default), Claude Code, Codex CLI, a plain terminal, or a custom command from **Setup**.
- Clone a project from an HTTPS or SSH Git URL.
- Create and initialize a new Git project.
- Import an existing repository by symlink, move, or copy; symlink is the default.
- Move a project to the desktop Trash, with an additional warning for uncommitted changes.

## Install

Install from the GitHub repository with Omarchy's plugin manager:

```bash
omarchy plugin add https://github.com/u64a/omarchy-project-launcher.git --enable
```

Open the launcher with:

```bash
omarchy-shell shell summon io.github.u64a.project-launcher '{}'
```

Start typing to filter by project name, branch, or status. Use **Up/Down** to select a repository and **Enter** to open your configured launcher in its working directory.

Update an existing installation with:

```bash
omarchy plugin update io.github.u64a.project-launcher
```

Your saved launcher choice is preserved. If the old interface remains visible after an update, run `omarchy restart shell` and reopen the launcher.

Remove the plugin with:

```bash
omarchy plugin remove io.github.u64a.project-launcher
```

For a convenient Omarchy menu entry, add this to `~/.config/omarchy/extensions/omarchy-menu.jsonc`:

```jsonc
"projects": {
  "icon": "󰲋",
  "label": "Projects",
  "description": "Open a CLI or terminal for a Git repository",
  "action": "omarchy-shell shell summon io.github.u64a.project-launcher '{}'"
},
```

## Manage projects

Select **+ Add** to:

- **Clone URL** — clone an HTTPS or SSH Git URL without recursively cloning submodules.
- **Create new** — create a folder and initialize a Git repository on `main`.
- **Import folder** — symlink (recommended), move, or copy an existing Git repository into the projects folder.

Each project row includes a **Trash** action. Projects are moved to the desktop Trash, never permanently deleted. Repositories with uncommitted changes require an additional confirmation, and imported symlinks are trashed without touching their external targets.

All controls are keyboard accessible. Press **Escape** to return to the project list or close the launcher.

## Configuration

### Launcher setup

Select **Setup** beside **+ Add**, choose a launcher, and press **Save**. Copilot remains the default until you save another choice.

![Launcher Setup showing Copilot, Claude Code, Codex CLI, Plain terminal, and Custom command](docs/setup.png)

| Option | Command required |
| --- | --- |
| GitHub Copilot CLI | `copilot` |
| Claude Code | `claude` |
| Codex CLI | `codex` |
| Plain terminal | No AI CLI required |
| Custom command | Your executable and arguments |

Unavailable launchers are marked **not installed**. Commands must be available in the Omarchy shell's `PATH`, or use an absolute executable path for a custom command. Nothing is installed automatically.

Every launcher starts in the selected repository's working directory. Custom commands support quoted arguments (for example, `my-tool --profile "work projects"`), but are not interpreted by a shell: pipes, redirects, environment-variable expansion, and command substitution are not supported. A leading `~` in the executable path is expanded.

Preferences are saved only when you press **Save**, in `${XDG_CONFIG_HOME:-~/.config}/omarchy-project-launcher/settings.json`, outside the plugin installation so updates preserve them. **Cancel** leaves the saved choice unchanged. Missing commands or invalid settings are reported in the app; open **Setup** to choose another launcher.

### Projects folder

By default, direct children of `~/Projects` are scanned each time the overlay opens. Set `OMARCHY_PROJECTS_ROOT` in the Omarchy shell environment to use another folder.

The launcher deliberately does not maintain a project registry or cache: adding or removing a repository is reflected the next time it opens.

## Requirements

- Omarchy Quattro with plugin support
- Python 3.10 or newer
- Git
- `xdg-terminal-exec`
- Your selected launcher (Copilot by default); no AI CLI is needed for **Plain terminal**

The plugin runs unsandboxed with normal user permissions, as all Omarchy plugins do. It reads Git metadata, can create/clone/import project folders at the user's request, and can move a selected project to the desktop Trash after confirmation. It never requires elevated privileges. Review [SECURITY.md](SECURITY.md) for the security model and reporting process.

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
