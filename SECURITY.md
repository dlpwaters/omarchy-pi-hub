# Security

Pi Hub is an unsandboxed Omarchy plugin. Its UI and helper run as the logged-in user. Installing Pi packages grants the installed extensions that same user-level access when Pi loads them. Do not install a source solely because its preview looks harmless.

## Protections and limits

Package review reads metadata and files without intentionally loading extensions. Installation requires an expiring token bound to the reviewed source, content fingerprint, and destination. npm versions and GitHub commits are pinned for review. The helper invokes programs using argument arrays and disables npm lifecycle scripts.

Review limits include a 10 MB compressed npm download, 500 archive entries, 100 MB declared archive expansion, 48 KB per file preview, and 500 KB of displayed preview text. Local package review rejects more than 500 files, symlinks, non-regular files, or more than 100 MB of hashed content; `.git` and dependency `node_modules` directories are excluded. Large, binary, or omitted previews and transitive dependencies require separate review. These bounds are resource controls, not malware detection.

### GitHub acquisition

Static GitHub review does not run Git, create a checkout, or write downloaded repository data to temporary storage. It resolves a branch/tag to a commit through GitHub's public API, obtains that commit's recursive tree, and downloads its codeload archive. All response bodies share a 10 MB budget, with at most one extra byte read to detect overflow. A 45-second wall-clock deadline covers the entire operation, including blocked network reads and archive inspection.

The tree must be complete and contain at most 500 entries (files and directories), with at most 100 MB of declared file content. Links, submodules, unsafe paths, and duplicate entries are rejected. The archive's decompressed stream is independently capped at 100 MB, including tar headers, PAX metadata, and padding, before tarfile can consume it. Files are streamed through Git blob hashing without extraction; every file must match the pinned tree's path, size, and blob hash. Missing/extra/transformed files, incomplete tar end markers, corrupt gzip trailers, and non-padding trailing data fail review. Archive buffers and previews are bounded in memory.

Checkout filters (including LFS) remain unsupported. Every `.gitattributes` file must be at most 48 KB and is checked in full; conservatively, any occurrence of `filter`, including in a comment, rejects the review. Archive export rules that omit or change tracked files also cause rejection. GitHub's unauthenticated API rate limits apply; no personal Git credentials or configuration are used for static review.

### Command cleanup

Pi package commands capture at most 1 MB of combined stdout/stderr. They run in a new process session with nonblocking pipe reads and an operation timeout. On overflow, timeout, error, or normal completion, the backend kills the command process group and reaps it. A Linux child subreaper also adopts and kills detached descendants. Cleanup has a two-second deadline and reports failure if it cannot finish; it does not report the operation as successful. These controls assume the backend's single-request, single-threaded process model. They do not sandbox deliberately hostile local executables or constrain the final Pi/npm installation's disk/network use.

A source is checked again before installation, but Pi and npm perform their own final downloads. Registry integrity, dependency resolution, trusted local executables, and the user's system configuration remain part of the trust boundary. Local packages remain mutable after review and installation. npm script suppression does not constrain an extension once it is loaded.

Editor operations reject stale revisions and writes outside managed resource roots, preserve backups, and use atomic replacement. Package changes preserve unrelated settings. These controls prevent common accidental mistakes; they do not isolate the plugin from another process already running as the same user.

No telemetry, authentication storage, network listener, root helper, or persistent background service is included. Network activity comes from requested npm searches, remote reviews, package operations, and explicitly opened web links. Recovery data remains under the user's Pi Hub state directory and may contain private resource text.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/dlpwaters/omarchy-pi-hub/security/advisories/new) for a reproducible security issue. Include the plugin version, relevant Omarchy/Pi versions, and a minimal demonstration using synthetic files. Do not include tokens, personal configuration, or private package contents.

Marketplace validation and this project's tests are not security certification. Compatibility has been tested on the versions in the README; other releases require validation.
