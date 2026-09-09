# Pi Hub status

Version 0.3.1 provides a native Omarchy panel with Browse, Installed, Extensions, Skills, and Prompts tabs. Repository: https://github.com/dlpwaters/omarchy-pi-hub.

The marketplace acquisition finding is addressed: static GitHub review uses bounded HTTP responses and a verified in-memory archive, with no Git fetch or temporary checkout. Commands enforce a combined output cap and terminate/reap their process group and detached descendants on failure or completion. Exact limits and compatibility exclusions are in SECURITY.md.

## Verification

- Backend regression suite: 35 passing tests, including acquisition/expansion/metadata limits, archive identity and completeness, Git filter rejection, output floods, timeouts, detached-child cleanup, and existing resource/package safety coverage.
- Python compilation, manifest parsing, QML lint, and Omarchy plugin validation pass.
- Isolated real-Pi acceptance passes on Pi 0.85.1: generated resources load through RPC without a model prompt; editor save/delete/restore and native resource toggles work; reviewed local install, package discovery, enable/disable, and removal work.
- The native panel has been exercised on Omarchy 4.0.2, Quickshell 0.3.1, and Qt 6.11.2. Native editing and confirmed package installation/removal were tested with temporary fixtures. npm browsing and source previews were checked live.
- Version 0.3.1 was checked through the existing Plugin Folders launcher: the native panel opened, displayed the 15-file GitHub review of the public v0.3.0 release pinned to `d9e609b`, finished without a backend error, and closed. The isolated real-Pi acceptance suite also passed with the new command runner.
- The README preview is a capture of the native interface with no personal path or resource contents displayed.

## Compatibility and boundaries

Python 3.11+ is required. A second physical Omarchy machine and older Omarchy/Pi versions have not been tested. GitHub Actions checks the portable backend; it does not exercise a compositor.

Existing Pi sessions need `/reload`. Project package changes use one-command Pi approval after the panel's explicit confirmation. npm lifecycle scripts are blocked; dependencies and final Pi/npm downloads retain their normal trust boundaries. Review cannot establish that a third-party package is safe.

Conventional Pi directories, shared `.agents/skills`, and common installed package layouts are supported. Unusual custom resource globs may need Pi's own configuration tools. Other Git hosts, SSH sources, Git checkout filters, and oversized local reviews are unsupported. See README and SECURITY.md for all dependencies, storage locations, and limits.

## Publication

Version 0.3.1 addresses the maintainer's requested fixes in [marketplace submission #5577](https://github.com/omacom/omarchy-plugin-marketplace/issues/5577). The README documents installation and removal, dependencies, storage, and safety boundaries. The next external step is maintainer re-review of the updated submission; listing requires their approval. See the repository's PRs and submission for current merge and automation status.
