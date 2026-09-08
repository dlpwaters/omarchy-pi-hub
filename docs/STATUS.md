# Pi Hub status

Version 0.3.0 provides a native Omarchy panel with Browse, Installed, Extensions, Skills, and Prompts tabs. Repository: https://github.com/dlpwaters/omarchy-pi-hub.

## Verification

- Backend regression suite: 23 passing tests, including stale-edit protection, scoped review tokens, source-change rejection, safe skill deletion, manifest path containment, review limits, and Git filter handling.
- Python compilation, manifest parsing, QML lint, and Omarchy plugin validation pass.
- Isolated real-Pi acceptance passes on Pi 0.85.1: generated resources load through RPC without a model prompt; editor save/delete/restore and native resource toggles work; reviewed local install, package discovery, enable/disable, and removal work.
- The native panel has been exercised on Omarchy 4.0.2, Quickshell 0.3.1, and Qt 6.11.2. Native editing and confirmed package installation/removal were tested with temporary fixtures. npm browsing and source previews were checked live.
- The README preview is a capture of the native interface with no personal path or resource contents displayed.

## Compatibility and boundaries

Python 3.11+ is required. A second physical Omarchy machine and older Omarchy/Pi versions have not been tested. GitHub Actions checks the portable backend; it does not exercise a compositor.

Existing Pi sessions need `/reload`. Project package changes use one-command Pi approval after the panel's explicit confirmation. npm lifecycle scripts are blocked; dependencies and final Pi/npm downloads retain their normal trust boundaries. Review cannot establish that a third-party package is safe.

Conventional Pi directories, shared `.agents/skills`, and common installed package layouts are supported. Unusual custom resource globs may need Pi's own configuration tools. Other Git hosts, SSH sources, Git checkout filters, and oversized local reviews are unsupported. See README and SECURITY.md for all dependencies, storage locations, and limits.

## Publication

Version 0.3.0 is prepared for public distribution from `main`. The README documents installation and removal, dependencies, storage, and safety boundaries. Marketplace submission follows release validation; listing requires separate maintainer approval. See the repository releases and marketplace submission for current publication status.
