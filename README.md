# Pi Hub

A native Omarchy panel for Pi packages, extensions, skills, and prompt templates. Click **π** in the bar to open it. Browsing, reviewing, editing, and managing resources happen inside the panel.

## Requirements

- Omarchy with the Quickshell `KeyboardPanel`, `Dropdown`, and plugin APIs.
- Python 3.11 or newer; the backend uses the standard library.
- [Pi](https://pi.dev) on the desktop session's `PATH`, plus npm and Git for package installation.
- `wl-copy` for copying commands or file content; `zenity` for the optional folder picker. A project folder can also be entered directly.

No provider API key, model call, background daemon, or companion Pi extension is required.

## Install

Clone this repository into the plugin directory:

```sh
git clone https://github.com/dlpwaters/omarchy-pi-hub.git \
  ~/.config/omarchy/plugins/io.github.dlpwaters.pi-hub
omarchy plugin validate ~/.config/omarchy/plugins/io.github.dlpwaters.pi-hub
omarchy plugin enable io.github.dlpwaters.pi-hub --section right
```

The repository is private during development. Access requires an authorized GitHub login. If upgrading an existing launcher, back up that plugin directory first instead of cloning over it. Omarchy normally reloads user plugin files; if it retains the old widget, run `omarchy restart shell`.

Open it from the bar, or use this command from a shortcut:

```sh
omarchy-shell io.github.dlpwaters.pi-hub toggle
```

The bundled `bin/pi-hub` runs that same native-panel command. The old `/pi-hub` Pi extension is independent and is no longer required. This plugin does not remove it or alter existing Pi sessions.

## Use

Choose **Global** for your personal Pi configuration, or **Project** and an existing absolute folder for that project's `.pi` directory. Check the displayed destination before making changes. Pi's own project trust requirements still apply.

- **Browse:** search npm's `pi-package` catalog, or paste an npm source, local path, or HTTPS GitHub URL. GitHub branches/tags are resolved to a commit before review. Review the README, manifest, warnings, and individual source files. Mark the source trusted, then confirm the exact installation and destination.
- **Installed:** select a package to review its files, review an update, enable or disable it, or remove it. Updates also go through review; Pi Hub never updates Pi itself.
- **Extensions:** inspect your local extension code and create command, custom-tool, or permission-hook starters. Saving an extension does not execute it. Adapt the starter's command/tool identifiers in the code before loading it.
- **Skills:** browse and edit `SKILL.md` files, or create a skill from a starter with the required name and description.
- **Prompts:** browse and edit Markdown templates. Code-review, debugging, and documentation starters are included. The filename becomes `/name`; Pi uses `$1`, `$2`, `$@`, and related positional argument forms.

Editors show the complete source so existing Markdown and frontmatter stay intact. Package-provided and external resources may be read-only; edit the original source or create your own local version. Save/discard prompts protect drafts when switching files, tabs, or scopes. Closing the panel keeps the current draft in memory; restarting the shell discards unsaved drafts.

Changes are available on Pi's next start. In an existing Pi session, use **Copy /reload** and paste it into Pi. The panel cannot force a reload into unrelated running Pi sessions. Copy command buttons provide `/skill:name` or `/name` for skills and prompts.

Keyboard shortcuts: **Ctrl+F** searches, **Ctrl+N** creates a resource in a library tab, **Ctrl+S** saves, and **Escape** closes the panel. Tab navigates controls.

## Review and safety

Package review reads metadata and source as data. Source content is displayed as plain text. Review is bounded and does not establish that a package or its dependencies are safe.

Installation requires a review token bound to the selected source and scope. Install operations block npm lifecycle scripts and use argument arrays rather than shell interpolation. Some packages require setup scripts and will need deliberate manual setup outside Pi Hub. Once Pi loads extensions, they can execute code with your user permissions. Skills and prompts can also direct an agent to run commands.

File writes use revision checks, backups, and atomic replacement. Paths outside managed resource locations are not writable through the editor. Deletes use recoverable trash with **Undo delete**. Package enable/disable preserves the original resource filters; native controls also enable/disable owned local resources. Settings changes preserve unrelated Pi options.

## Development and verification

```sh
./check.sh
```

To additionally exercise the real Pi CLI without model calls or personal config changes:

```sh
PI_HUB_PI="$(mise which pi)" python3 scripts/acceptance.py
```

`PI_HUB_PI` should be the actual Pi executable, not a version-manager shim, because the acceptance test isolates `HOME`. For other installations, use the absolute executable path.

`check.sh` runs the backend's isolated tests, Python compilation, manifest parsing, QML lint, and Omarchy plugin validation. GitHub Actions runs the portable backend checks. Native shell acceptance must be checked on an Omarchy desktop:

```sh
omarchy-shell io.github.dlpwaters.pi-hub open
omarchy-shell io.github.dlpwaters.pi-hub status
omarchy-shell io.github.dlpwaters.pi-hub tab Skills
omarchy-shell io.github.dlpwaters.pi-hub close
```

The backend accepts one JSON request on stdin through `python3 hub.py rpc`. `Panel.qml` owns editor and review state; `BarWidget.qml` owns bar integration and IPC. Configuration paths are derived from the current user and selected project, with no developer home directory embedded in the plugin.

See [docs/STATUS.md](docs/STATUS.md) for verified behavior and remaining release work. Pi's [package documentation](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/packages.md) and the installed Pi docs define package/resource behavior.
