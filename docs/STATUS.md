# Pi Hub status

Native Omarchy plugin, version 0.2.0. The π bar button and bundled launcher open a Quickshell panel with Browse, Installed, Extensions, Skills, and Prompts tabs.

## Verified

- 15 isolated backend regression tests pass.
- Python compilation, manifest parsing, QML lint, and `omarchy plugin validate` pass.
- `scripts/acceptance.py` passes against Pi 0.85.1 using an isolated home/project and no model prompts: generated resource loading through RPC, create/edit/stale-revision rejection/delete/restore, resource enable/disable, reviewed installation, package discovery/enable/disable/removal, scope-bound review tokens, and changed-source rejection.
- The native panel opens and reads the user's existing resources; live npm catalog search works.
- Native Ctrl+N, text entry, Ctrl+S, disk save, and skill selection/read were exercised in a temporary project. Native review/trust/install confirmation and removal confirmation also passed with a harmless local package.
- Live npm static review includes the README, manifest, and prioritized source previews. Plain GitHub URL review resolves to an immutable commit before inspection.

## Operational notes

- Existing Pi sessions need `/reload`; the panel provides a copy button. It does not inject commands into another process or call a model.
- Project package changes use Pi's documented one-command `--approve` after the panel's explicit review/confirmation. This does not persist project trust. Global operations use `--no-approve` to avoid loading a project's configuration.
- Npm lifecycle scripts are blocked. Packages needing setup scripts require deliberate manual setup. The reviewed archive is checked again before installation; the final Pi/npm download retains the normal registry trust boundary.
- Reviews have bounded downloads and previews: 10 MB compressed download, 500 archive entries, 100 MB declared archive expansion, 48 KB per file preview, 500 KB total preview text, and 30-minute review tokens.
- Local and npm packages, HTTPS GitHub URLs, GitHub branches/tags, and pinned commits are supported. Other Git hosts and SSH sources are not supported by the native reviewer.
- Resource discovery covers conventional Pi directories, shared `.agents/skills`, and common installed package layouts. Package and shared resources are read-only. Unusual resource layouts configured with custom globs may need Pi's own configuration tools.
- File/config backups, review tokens, private locks, and recoverable trash live under `$XDG_STATE_HOME/pi-hub` (default `~/.local/state/pi-hub`). UI scope/folder preferences live in `~/.config/omarchy/pi-hub.ini`.
- Unsaved editor drafts survive panel close/open, but not a shell restart. File revision checks reject overwriting an external edit.
- The old standalone Pi Hub extension is preserved and is no longer required by this plugin.

## Handoff

Repository: https://github.com/dlpwaters/omarchy-pi-hub (private). Initial development branch: `main`.

Next release step: user acceptance on normal day-to-day packages and a second Omarchy installation, then prepare a public release and marketplace submission with explicit publication approval. No marketplace submission or public release has been made.
