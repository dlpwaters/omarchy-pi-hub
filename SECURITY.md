# Security

Pi Hub is an unsandboxed Omarchy plugin. Its UI and helper run as the logged-in user. Installing Pi packages grants the installed extensions that same user-level access when Pi loads them. Do not install a source solely because its preview looks harmless.

## Protections and limits

Package review reads metadata and files without intentionally loading extensions. Installation requires an expiring token bound to the reviewed source, content fingerprint, and destination. npm versions and GitHub commits are pinned for review. The helper invokes programs using argument arrays and disables npm lifecycle scripts.

Review limits include a 10 MB compressed npm download, 500 archive entries, 100 MB declared archive expansion, 48 KB per file preview, and 500 KB of preview text. Local package review rejects more than 500 files, symlinks, non-regular files, or more than 100 MB of hashed content; `.git` and dependency `node_modules` directories are excluded. Git fetches have time limits but do not have a byte quota. Static GitHub review ignores personal Git configuration and rejects checkout filters, including LFS, to avoid reviewing a pointer while installing different content. Large, binary, or omitted files and transitive dependencies require separate review. These bounds are resource controls, not malware detection.

A source is checked again before installation, but Pi and npm perform their own final downloads. Registry integrity, dependency resolution, trusted local executables, and the user's system configuration remain part of the trust boundary. Local packages remain mutable after review and installation. npm script suppression does not constrain an extension once it is loaded.

Editor operations reject stale revisions and writes outside managed resource roots, preserve backups, and use atomic replacement. Package changes preserve unrelated settings. These controls prevent common accidental mistakes; they do not isolate the plugin from another process already running as the same user.

No telemetry, authentication storage, network listener, root helper, or persistent background service is included. Network activity comes from requested npm searches, remote reviews, package operations, and explicitly opened web links. Recovery data remains under the user's Pi Hub state directory and may contain private resource text.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/dlpwaters/omarchy-pi-hub/security/advisories/new) for a reproducible security issue. Include the plugin version, relevant Omarchy/Pi versions, and a minimal demonstration using synthetic files. Do not include tokens, personal configuration, or private package contents.

Marketplace validation and this project's tests are not security certification. Compatibility has been tested on the versions in the README; other releases require validation.
