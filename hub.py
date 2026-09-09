#!/usr/bin/env python3
"""Small, non-interactive backend for the native Pi Hub UI.

The process accepts one JSON request on stdin and emits one JSON response.  It
does not load Pi extensions or invoke a model; Pi is only called for an
explicit, previously reviewed install or remove operation.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import fcntl
import fnmatch
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


RESOURCE_KINDS = ("extensions", "skills", "prompts")
PACKAGE_KINDS = ("extensions", "skills", "prompts", "themes")
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
NPM_NAME_RE = re.compile(r"^(?:@[a-z0-9._~-]+/[a-z0-9._~-]+|[a-z0-9._~-]+)$", re.I)
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
TEXT_SUFFIXES = {".ts", ".js", ".mjs", ".cjs", ".md", ".json", ".jsonc", ".txt", ".yaml", ".yml", ".sh", ".bash", ".py", ".toml", ".lua"}
MAX_FILE = 1_000_000
MAX_ARCHIVE = 10_000_000
MAX_REVIEW_TEXT = 500_000
MAX_PREVIEW_FILE = 48_000
MAX_REVIEW_FILES = 500
MAX_ARCHIVE_UNCOMPRESSED = 100_000_000
MAX_COMMAND_OUTPUT = 1_000_000
GITHUB_REVIEW_TIMEOUT = 45
REVIEW_TTL = 30 * 60


class HubError(Exception):
    pass


class HttpsRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != "https":
            raise HubError("Refusing a package download redirect outside HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urlopen(request, timeout):
    return build_opener(HttpsRedirectHandler()).open(request, timeout=timeout)


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home()))).expanduser().resolve()


def _global_agent_dir() -> Path:
    raw = os.environ.get("PI_CODING_AGENT_DIR")
    path = Path(raw).expanduser() if raw else _home() / ".pi" / "agent"
    return Path(os.path.abspath(path))


def _state_file() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", str(_home() / ".local" / "state"))).expanduser()
    path = Path(os.path.abspath(base / "pi-hub" / "state.json"))
    _assert_no_symlink(path, path.parent)
    return path


def _scope(request: dict[str, Any]) -> dict[str, Any]:
    scope = request.get("scope")
    if scope not in ("global", "project"):
        raise HubError("scope must be 'global' or 'project'")
    raw_project = request.get("project")
    if not isinstance(raw_project, str) or not raw_project or not os.path.isabs(raw_project):
        raise HubError("project must be an absolute existing folder")
    project = Path(raw_project)
    if not project.is_dir():
        raise HubError("project must be an absolute existing folder")
    project = project.resolve()
    agent_dir = _global_agent_dir() if scope == "global" else project / ".pi"
    return {
        "scope": scope,
        "project": project,
        "agent_dir": agent_dir,
        "settings": agent_dir / "settings.json",
        "scopePath": str(agent_dir),
        "key": f"{scope}:{agent_dir.resolve()}",
    }


def _json_read(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    if path.is_symlink():
        raise HubError(f"Refusing to read symlink: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise HubError(f"Cannot read {path}: {exc}") from exc


def _assert_no_symlink(path: Path, root: Path) -> None:
    root_abs = Path(os.path.abspath(root))
    path_abs = Path(os.path.abspath(path))
    try:
        path_abs.relative_to(root_abs)
    except ValueError as exc:
        raise HubError("Path is outside the selected Pi resource directory") from exc
    current = Path(root_abs.anchor)
    for part in root_abs.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise HubError(f"Refusing to write through symlink: {current}")
    for part in path_abs.relative_to(root_abs).parts:
        current = current / part
        if current.is_symlink():
            raise HubError(f"Refusing to write through symlink: {current}")


@contextlib.contextmanager
def _file_lock(target: Path):
    lock_dir = _state_file().parent / "locks"
    _assert_no_symlink(lock_dir, lock_dir.parent)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_dir / (hashlib.sha256(str(target.absolute()).encode()).hexdigest() + ".lock")
    _assert_no_symlink(lock, lock_dir)
    with lock.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    _assert_no_symlink(path, path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _json_update(path: Path, change: Callable[[dict[str, Any]], Any], backup_dir: Path | None = None, pi_lock: bool = False) -> Any:
    with _file_lock(path):
        native_lock = Path(str(path) + ".lock") if pi_lock else None
        if native_lock is not None:
            native_lock.parent.mkdir(parents=True, exist_ok=True)
            try:
                native_lock.mkdir()
            except FileExistsError as exc:
                raise HubError("Pi settings are busy; try again after Pi finishes writing them") from exc
        try:
            value = _json_read(path, {})
            if not isinstance(value, dict):
                raise HubError(f"Expected a JSON object in {path}")
            result = change(value)
            if backup_dir is not None and path.exists():
                _assert_no_symlink(backup_dir, backup_dir)
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup = backup_dir / f"{int(time.time() * 1000)}-{secrets.token_hex(3)}-{path.name}"
                shutil.copy2(path, backup, follow_symlinks=False)
            _atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode())
            return result
        finally:
            if native_lock is not None:
                with contextlib.suppress(OSError):
                    native_lock.rmdir()


def _state_update(change: Callable[[dict[str, Any]], Any]) -> Any:
    return _json_update(_state_file(), change)


def _settings_update(scope: dict[str, Any], change: Callable[[dict[str, Any]], Any]) -> Any:
    _assert_no_symlink(scope["settings"], scope["agent_dir"])
    backup_dir = _state_file().parent / "backups" / hashlib.sha256(scope["key"].encode()).hexdigest()[:12] / "settings"
    return _json_update(scope["settings"], change, backup_dir, pi_lock=True)


def _read_text(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE + 1)
    except OSError as exc:
        raise HubError(f"Cannot read {path}: {exc}") from exc
    if len(data) > MAX_FILE:
        raise HubError("Resource is too large to edit in Pi Hub")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HubError("Resource is not UTF-8 text") from exc


def _revision(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _frontmatter(body: str) -> dict[str, str]:
    if not body.startswith("---\n"):
        return {}
    end = body.find("\n---", 4)
    if end < 0:
        return {}
    values: dict[str, str] = {}
    lines = body[4:end].splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if ":" not in line or line[:1].isspace():
            index += 1
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if value in (">", "|"):
            continuation = []
            index += 1
            while index < len(lines) and (lines[index][:1].isspace() or not lines[index].strip()):
                if lines[index].strip():
                    continuation.append(lines[index].strip())
                index += 1
            values[key.strip()] = (" " if value == ">" else "\n").join(continuation)
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
        index += 1
    return values


def _description(body: str) -> str:
    meta = _frontmatter(body)
    if meta.get("description"):
        return meta["description"]
    in_frontmatter = body.startswith("---\n")
    passed = not in_frontmatter
    for line in body.splitlines():
        if in_frontmatter and line == "---":
            if passed:
                continue
            if line != body.splitlines()[0]:
                passed = True
            continue
        if passed and line.strip():
            return line.strip().lstrip("# ")
    return ""


def _validate_skill(body: str) -> tuple[str, str]:
    meta = _frontmatter(body)
    name = meta.get("name", "")
    description = meta.get("description", "")
    if not NAME_RE.fullmatch(name):
        raise HubError("Skill frontmatter needs a 1-64 character lowercase hyphenated name")
    if not description or len(description) > 1024:
        raise HubError("Skill frontmatter needs a non-empty description up to 1024 characters")
    return name, description


def _validate_name(name: Any) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise HubError("name must use lowercase letters, numbers, and single hyphens (1-64 characters)")
    return name


def _package_source(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("source"), str):
        return entry["source"]
    return None


def _package_name(source: str) -> str:
    if source.startswith("npm:"):
        spec = source[4:]
        if spec.startswith("@"):
            slash = spec.find("/")
            version_at = spec.find("@", slash + 1)
        else:
            version_at = spec.rfind("@")
        return spec if version_at <= 0 else spec[:version_at]
    if source.startswith(("git:", "https://", "ssh://")):
        base = source.rsplit("@", 1)[0]
        return base.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    return Path(source).name or source


def _filters(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {kind: None for kind in PACKAGE_KINDS}
    return {kind: entry.get(kind) if kind in entry else None for kind in PACKAGE_KINDS}


def _package_enabled(entry: Any) -> bool:
    filters = _filters(entry)
    return not all(filters[kind] == [] for kind in PACKAGE_KINDS)


def _settings(scope: dict[str, Any]) -> dict[str, Any]:
    value = _json_read(scope["settings"], {})
    if not isinstance(value, dict):
        raise HubError("Pi settings must contain a JSON object")
    return value


def _resource_enabled(settings: dict[str, Any], kind: str, path: Path) -> bool:
    items = settings.get(kind, [])
    if not isinstance(items, list):
        return True
    exact = str(path.resolve())
    enabled = True
    for item in items:
        if item == "-" + exact or item == "!" + exact:
            enabled = False
        elif item == "+" + exact or item == exact:
            enabled = True
    return enabled


def _resource(kind: str, path: Path, editable: bool, enabled: bool) -> dict[str, Any]:
    try:
        body = _read_text(path)
    except HubError:
        body = ""
    if kind == "skills":
        name = _frontmatter(body).get("name") or (path.parent.name if path.name == "SKILL.md" else path.stem)
    elif kind == "extensions" and path.stem == "index":
        name = path.parent.name
    else:
        name = path.stem
    return {
        "kind": kind,
        "name": name,
        "description": _description(body),
        "path": str(path.resolve()),
        "editable": editable,
        "enabled": enabled,
    }


def _safe_files(root: Path, pattern: str) -> Iterable[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    result = []
    for path in root.glob(pattern):
        if path.is_file() and not path.is_symlink():
            result.append(path)
    return sorted(result, key=lambda p: str(p))


def _scan_skill_root(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    found: set[Path] = set()
    for path in _safe_files(root, "**/SKILL.md"):
        found.add(path)
    for path in _safe_files(root, "*.md"):
        if _frontmatter(_read_text(path)).get("description"):
            found.add(path)
    # Shared skill roots allow nested standalone markdown skills.
    if root.name == "skills" and root.parent.name == ".agents":
        for path in _safe_files(root, "*/*.md"):
            if _frontmatter(_read_text(path)).get("description"):
                found.add(path)
    return sorted(found, key=lambda p: str(p))


def _package_roots(source: str, scope: dict[str, Any]) -> list[Path]:
    roots: list[Path] = []
    if source.startswith("/") or source.startswith("./") or source.startswith("../"):
        path = Path(source)
        if not path.is_absolute():
            path = scope["settings"].parent / path
        path = path.resolve()
        if (path.is_dir() or path.suffix in (".ts", ".js")) and path.exists() and not path.is_symlink():
            roots.append(path)
        return roots
    if source.startswith(("git:", "https://github.com/")):
        try:
            url, _ = _parse_github(source)
        except HubError:
            return roots
        parsed = urlparse(url)
        relative = parsed.path.lstrip("/")
        for suffix in (relative.removesuffix(".git"), relative):
            path = scope["agent_dir"] / "git" / parsed.hostname / suffix
            if path.is_dir() and not path.is_symlink():
                roots.append(path)
                break
        return roots
    if not source.startswith("npm:"):
        return roots
    wanted = _package_name(source)
    npm_root = scope["agent_dir"] / "npm"
    if not npm_root.is_dir() or npm_root.is_symlink():
        return roots
    if NPM_NAME_RE.fullmatch(wanted):
        direct = npm_root / "node_modules" / wanted
        manifest_file = direct / "package.json"
        if direct.is_dir() and not direct.is_symlink() and manifest_file.is_file():
            manifest = _json_read(manifest_file, {})
            if isinstance(manifest, dict) and manifest.get("name") == wanted:
                return [direct]
    seen = 0
    for current, dirs, files in os.walk(npm_root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink()]
        seen += 1
        if seen > 400:
            break
        if "package.json" not in files:
            continue
        package_file = Path(current) / "package.json"
        try:
            manifest = _json_read(package_file, {})
        except HubError:
            continue
        if isinstance(manifest, dict) and manifest.get("name") == wanted:
            roots.append(Path(current).resolve())
            break
    return roots


def _manifest_paths(root: Path, manifest: dict[str, Any], kind: str) -> list[Path]:
    pi = manifest.get("pi") if isinstance(manifest.get("pi"), dict) else {}
    rules = pi.get(kind)
    if isinstance(rules, str):
        rules = [rules]
    result: list[Path] = []
    if isinstance(rules, list):
        exclusions = [rule[1:] for rule in rules if isinstance(rule, str) and rule.startswith("!")]
        for rule in rules:
            if not isinstance(rule, str) or rule.startswith(("!", "+", "-")):
                continue
            if Path(rule).is_absolute() or ".." in Path(rule).parts:
                continue
            candidate = root / rule
            if not candidate.resolve().is_relative_to(root.resolve()):
                continue
            if not any(c in rule for c in "*?[") and candidate.is_file() and not candidate.is_symlink():
                result.append(candidate)
            else:
                glob = rule if any(c in rule for c in "*?[") else rule.rstrip("/") + "/**/*"
                result.extend(_safe_files(root, glob))
        result = [
            path for path in result
            if not any(fnmatch.fnmatch(path.relative_to(root).as_posix(), exclusion) for exclusion in exclusions)
        ]
    else:
        folder = root / kind
        if kind == "skills":
            return _scan_skill_root(folder)
        suffixes = {"extensions": {".ts", ".js"}, "prompts": {".md"}}[kind]
        result.extend(p for p in _safe_files(folder, "**/*") if p.suffix in suffixes)
    suffixes = {"extensions": {".ts", ".js"}, "skills": {".md"}, "prompts": {".md"}}[kind]
    return sorted({p for p in result if p.suffix in suffixes}, key=lambda p: str(p))


def action_list(request: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    settings = _settings(scope)
    package_entries = settings.get("packages", [])
    if not isinstance(package_entries, list):
        raise HubError("packages in Pi settings must be an array")
    packages = []
    resources: list[dict[str, Any]] = []
    seen: set[Path] = set()

    def add(kind: str, path: Path, editable: bool, enabled: bool | None = None) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        resources.append(_resource(kind, path, editable, _resource_enabled(settings, kind, path) if enabled is None else enabled))

    owned = scope["agent_dir"]
    extension_files = list(_safe_files(owned / "extensions", "*"))
    extension_files += list(_safe_files(owned / "extensions", "*/index.ts"))
    extension_files += list(_safe_files(owned / "extensions", "*/index.js"))
    for path in extension_files:
        if path.suffix in (".ts", ".js"):
            add("extensions", path, True)
    for path in _scan_skill_root(owned / "skills"):
        add("skills", path, True)
    for path in _safe_files(owned / "prompts", "*.md"):
        add("prompts", path, True)

    shared_roots = [_home() / ".agents" / "skills"]
    if scope["scope"] == "project":
        shared_roots.append(scope["project"] / ".agents" / "skills")
    for root in shared_roots:
        for path in _scan_skill_root(root):
            add("skills", path, False)

    for entry in package_entries:
        source = _package_source(entry)
        if source is None:
            continue
        package = {
            "source": source,
            "name": _package_name(source),
            "enabled": _package_enabled(entry),
            "filters": _filters(entry),
        }
        packages.append(package)
        for root in _package_roots(source, scope):
            if root.is_file():
                add("extensions", root, False, package["enabled"] and _filter_allows(package["filters"].get("extensions"), root.name))
                continue
            manifest = _json_read(root / "package.json", {}) if (root / "package.json").is_file() else {}
            if not isinstance(manifest, dict):
                manifest = {}
            for kind in RESOURCE_KINDS:
                package_filters = package["filters"].get(kind)
                for path in _manifest_paths(root, manifest, kind):
                    rel = path.relative_to(root).as_posix()
                    enabled = package["enabled"] and _filter_allows(package_filters, rel)
                    add(kind, path, False, enabled)

    resources.sort(key=lambda item: (item["kind"], item["name"].lower(), item["path"]))
    result: dict[str, Any] = {
        "packages": packages,
        "resources": resources,
        "piAvailable": _pi_path() is not None,
        "scopePath": scope["scopePath"],
    }
    if not packages and not resources:
        result["message"] = "No Pi packages or resources found in this scope."
    return result


def _filter_allows(filters: Any, relative: str) -> bool:
    if filters is None:
        return True
    if not isinstance(filters, list) or not filters:
        return False
    allowed = False
    for rule in filters:
        if not isinstance(rule, str):
            continue
        if rule == "+" + relative:
            allowed = True
        elif rule == "-" + relative:
            allowed = False
        elif rule.startswith("!") and fnmatch.fnmatch(relative, rule[1:]):
            allowed = False
        elif not rule.startswith(("+", "-", "!")) and fnmatch.fnmatch(relative, rule):
            allowed = True
    return allowed


def _open_json(url: str, timeout: int = 8) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "omarchy-pi-hub/0.1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_ARCHIVE + 1)
    except Exception as exc:
        raise HubError(f"Network request failed: {exc}") from exc
    if len(data) > MAX_ARCHIVE:
        raise HubError("Registry response is too large")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HubError("Registry returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise HubError("Registry returned an unexpected response")
    return value


def action_search(request: dict[str, Any], _scope_data: dict[str, Any]) -> dict[str, Any]:
    query = request.get("query", "")
    offset = request.get("offset", 0)
    if not isinstance(query, str) or len(query.strip()) > 200:
        raise HubError("query must be a string up to 200 characters")
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 10_000:
        raise HubError("offset must be an integer from 0 to 10000")
    terms = "keywords:pi-package"
    if query.strip():
        terms += " " + query.strip()
    url = "https://registry.npmjs.org/-/v1/search?" + urlencode({"text": terms, "size": 30, "from": offset})
    data = _open_json(url)
    results = []
    for item in data.get("objects", []):
        package = item.get("package", {}) if isinstance(item, dict) else {}
        name, version = package.get("name"), package.get("version")
        if isinstance(name, str) and isinstance(version, str):
            results.append({
                "source": f"npm:{name}@{version}",
                "name": name,
                "description": package.get("description", "") if isinstance(package.get("description", ""), str) else "",
                "version": version,
            })
    total = data.get("total", len(results))
    return {"results": results, "total": total if isinstance(total, int) else len(results), "offset": offset}


def _split_npm(source: str) -> tuple[str, str | None]:
    spec = source.removeprefix("npm:")
    if spec.startswith("@"):
        slash = spec.find("/")
        at = spec.find("@", slash + 1)
    else:
        at = spec.rfind("@")
    name, requested = (spec, None) if at <= 0 else (spec[:at], spec[at + 1 :])
    if not NPM_NAME_RE.fullmatch(name) or (requested is not None and not requested):
        raise HubError("Invalid npm package source")
    return name, requested


def _fetch_bytes(url: str, timeout: int = 12) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HubError("Package archive must use HTTPS")
    try:
        with urlopen(Request(url, headers={"User-Agent": "omarchy-pi-hub/0.1"}), timeout=timeout) as response:
            data = response.read(MAX_ARCHIVE + 1)
    except Exception as exc:
        raise HubError(f"Package download failed: {exc}") from exc
    if len(data) > MAX_ARCHIVE:
        raise HubError("Package archive is too large to review")
    return data


def _verify_archive(data: bytes, dist: dict[str, Any]) -> None:
    integrity = dist.get("integrity")
    if isinstance(integrity, str):
        for algorithm in ("sha512", "sha384", "sha256", "sha1"):
            candidates = [c.split("-", 1)[1] for c in integrity.split() if c.startswith(algorithm + "-")]
            if candidates:
                actual = base64.b64encode(hashlib.new(algorithm, data).digest()).decode()
                if not any(secrets.compare_digest(actual.rstrip("="), value.rstrip("=")) for value in candidates):
                    raise HubError("Package archive integrity check failed")
                return
    shasum = dist.get("shasum")
    if isinstance(shasum, str) and re.fullmatch(r"[0-9a-fA-F]{40}", shasum):
        if not secrets.compare_digest(hashlib.sha1(data).hexdigest(), shasum.lower()):
            raise HubError("Package archive checksum check failed")
        return
    raise HubError("Package metadata has no supported archive checksum")


def _text_preview(data: bytes, remaining: int) -> str:
    text = data.decode("utf-8", errors="replace")
    limit = min(remaining, MAX_PREVIEW_FILE)
    if len(text) > limit:
        return text[:limit] + "\n… [truncated by Pi Hub]"
    return text


def _previewable(name: str) -> bool:
    path = Path(name)
    return path.suffix.lower() in TEXT_SUFFIXES or path.name.lower() in {"readme", "license", "licence", "copying", "makefile", ".gitignore"}


def _preview_priority(name: str) -> tuple[int, str]:
    lowered = name.lower()
    basename = Path(lowered).name
    if basename == "package.json":
        rank = 0
    elif basename.startswith("readme"):
        rank = 1
    elif lowered.startswith(("extensions/", "skills/", "prompts/")) or "/extensions/" in lowered or "/skills/" in lowered or "/prompts/" in lowered:
        rank = 2
    elif basename.startswith(("changelog", "changes", "history")):
        rank = 5
    else:
        rank = 3
    return rank, lowered


def _inspect_tarball(data: bytes) -> tuple[list[str], dict[str, str], list[str]]:
    files: list[str] = []
    contents: dict[str, str] = {}
    warnings: list[str] = []
    total_text = 0
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r|gz")
    except tarfile.TarError as exc:
        raise HubError("Package archive is not a valid npm tarball") from exc
    with archive:
        preview_data: list[tuple[str, bytes, int]] = []
        uncompressed = 0
        entries = 0
        for member in archive:
            entries += 1
            if entries > MAX_REVIEW_FILES:
                warnings.append("Archive has more than 500 entries; the file list was truncated.")
                break
            uncompressed += max(member.size, 0)
            if uncompressed > MAX_ARCHIVE_UNCOMPRESSED:
                raise HubError("Package archive expands beyond the 100 MB review limit")
            parts = Path(member.name).parts
            if member.name.startswith("/") or ".." in parts:
                warnings.append(f"Unsafe archive path omitted: {member.name}")
                continue
            name = "/".join(parts[1:]) if parts[:1] == ("package",) else "/".join(parts)
            if not name or member.isdir():
                continue
            files.append(name)
            if member.issym() or member.islnk():
                warnings.append(f"Archive contains a link: {name}")
                continue
            if not member.isfile() or not _previewable(name):
                continue
            if member.size > MAX_FILE:
                warnings.append(f"Large file omitted from preview: {name}")
                continue
            stream = archive.extractfile(member)
            if stream is not None:
                preview_data.append((name, stream.read(MAX_PREVIEW_FILE + 1), member.size))
        for name, raw, declared_size in sorted(preview_data, key=lambda item: _preview_priority(item[0])):
            if total_text >= MAX_REVIEW_TEXT:
                warnings.append("Some text files have no preview because the 500 KB review limit was reached.")
                break
            remaining = MAX_REVIEW_TEXT - total_text
            preview = _text_preview(raw, remaining)
            contents[name] = preview
            total_text += len(preview)
            if declared_size > MAX_PREVIEW_FILE:
                warnings.append(f"Preview truncated to 48 KB: {name}")
    return sorted(files), contents, warnings


def _walk_static(root: Path, strict: bool = False) -> tuple[list[str], dict[str, str], list[str]]:
    files: list[str] = []
    contents: dict[str, str] = {}
    warnings: list[str] = []
    total_text = 0
    candidates: list[tuple[str, Path]] = []
    for current, dirs, names in os.walk(root, followlinks=False):
        if strict and any((Path(current) / d).is_symlink() for d in dirs if d not in (".git", "node_modules")):
            raise HubError("Local package contains a symlinked directory; review a regular-file copy")
        dirs[:] = sorted(d for d in dirs if d not in (".git", "node_modules") and not (Path(current) / d).is_symlink())
        for filename in sorted(names):
            path = Path(current) / filename
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                if strict:
                    raise HubError("Local package contains a symlink; review a regular-file copy")
                warnings.append(f"Link omitted from preview: {rel}")
                continue
            if not path.is_file():
                if strict:
                    raise HubError("Local package contains a non-regular file")
                continue
            if len(files) >= MAX_REVIEW_FILES:
                if strict:
                    raise HubError("Local package exceeds the 500-file review limit")
                warnings.append("Package has more than 500 files; the file list was truncated.")
                break
            files.append(rel)
            if not _previewable(rel):
                continue
            if path.stat().st_size > MAX_FILE:
                warnings.append(f"Large file omitted from preview: {rel}")
                continue
            candidates.append((rel, path))
        if not strict and len(files) >= MAX_REVIEW_FILES:
            break
    for rel, path in sorted(candidates, key=lambda item: _preview_priority(item[0])):
        if total_text >= MAX_REVIEW_TEXT:
            warnings.append("Some text files have no preview because the 500 KB review limit was reached.")
            break
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_FILE + 1)
        except OSError:
            continue
        preview = _text_preview(raw, MAX_REVIEW_TEXT - total_text)
        if len(raw.decode("utf-8", errors="replace")) > MAX_PREVIEW_FILE:
            warnings.append(f"Preview truncated to 48 KB: {rel}")
            contents[rel] = preview
        else:
            contents[rel] = preview
        total_text += len(preview)
    return files, contents, warnings


def _manifest_warnings(manifest: dict[str, Any], extra: list[str]) -> list[str]:
    warnings = [
        "Pi packages run with your full user access; extensions can execute arbitrary code and skills can direct tool use."
    ]
    scripts = manifest.get("scripts")
    if isinstance(scripts, dict) and scripts:
        warnings.append("This package declares npm install or lifecycle scripts. Pi Hub installs with npm scripts disabled.")
    dependencies = manifest.get("dependencies")
    if isinstance(dependencies, dict) and dependencies:
        warnings.append("Package dependencies are not included in this static review and may contain executable code.")
    warnings.extend(extra)
    return list(dict.fromkeys(warnings))


def _review_payload(source: str, name: str, version: str, files: list[str], contents: dict[str, str], extra_warnings: list[str], artifact_digest: str) -> dict[str, Any]:
    manifest_text = contents.get("package.json", "")
    if not manifest_text:
        manifest_text = contents.get("package/package.json", "")
    manifest: dict[str, Any] = {}
    if manifest_text:
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(manifest_text)
            if isinstance(parsed, dict):
                manifest = parsed
    readme = ""
    for filename, text in contents.items():
        if Path(filename).name.lower().startswith("readme") and Path(filename).suffix.lower() in (".md", ".txt", ""):
            readme = text
            break
    digest_value = {
        "source": source,
        "version": version,
        "files": files,
        "fileContents": contents,
        "artifactDigest": artifact_digest,
    }
    digest = hashlib.sha256(json.dumps(digest_value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "source": source,
        "name": name,
        "description": manifest.get("description", "") if isinstance(manifest.get("description", ""), str) else "",
        "version": version,
        "readme": readme,
        "manifest": manifest_text,
        "warnings": _manifest_warnings(manifest, extra_warnings),
        "files": files,
        "fileContents": contents,
        "_digest": digest,
    }


def _review_npm(source: str) -> dict[str, Any]:
    name, requested = _split_npm(source)
    metadata = _open_json("https://registry.npmjs.org/" + quote(name, safe="@"))
    if requested and VERSION_RE.fullmatch(requested):
        version = requested
    else:
        tag = requested or "latest"
        tags = metadata.get("dist-tags", {})
        version = tags.get(tag) if isinstance(tags, dict) else None
    versions = metadata.get("versions", {})
    manifest = versions.get(version) if isinstance(versions, dict) and isinstance(version, str) else None
    if not isinstance(manifest, dict):
        raise HubError("npm source must resolve to an exact published version or dist-tag")
    dist = manifest.get("dist", {})
    tarball = dist.get("tarball") if isinstance(dist, dict) else None
    if not isinstance(tarball, str):
        raise HubError("npm registry metadata has no package archive")
    data = _fetch_bytes(tarball)
    _verify_archive(data, dist)
    files, contents, warnings = _inspect_tarball(data)
    exact = f"npm:{name}@{version}"
    review = _review_payload(exact, name, version, files, contents, warnings, hashlib.sha256(data).hexdigest())
    if not review["manifest"]:
        review["manifest"] = json.dumps(manifest, indent=2, ensure_ascii=False)
        review["description"] = manifest.get("description", "") if isinstance(manifest.get("description", ""), str) else ""
    return review


def _review_local(source: str, scope: dict[str, Any]) -> dict[str, Any]:
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = scope["project"] / path
    if path.is_symlink():
        raise HubError("Refusing to review a symlinked package")
    path = path.resolve()
    if not path.exists():
        raise HubError("Local package does not exist")
    if path.is_file():
        with path.open("rb") as handle:
            raw = handle.read(MAX_FILE + 1)
        if len(raw) > MAX_FILE:
            raise HubError("Local extension is too large to review")
        contents = {path.name: _text_preview(raw, MAX_REVIEW_TEXT)}
        return _review_payload(str(path), path.stem, "local", [path.name], contents, [], hashlib.sha256(raw).hexdigest())
    files, contents, warnings = _walk_static(path, strict=True)
    artifact_hash = hashlib.sha256()
    total_bytes = 0
    for relative in files:
        candidate = path / relative
        if candidate.is_file() and not candidate.is_symlink():
            artifact_hash.update(relative.encode("utf-8") + b"\0")
            with candidate.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_ARCHIVE_UNCOMPRESSED:
                        raise HubError("Local package exceeds the 100 MB review limit")
                    artifact_hash.update(chunk)
    manifest: dict[str, Any] = {}
    with contextlib.suppress(json.JSONDecodeError):
        parsed = json.loads(contents.get("package.json", "{}"))
        if isinstance(parsed, dict):
            manifest = parsed
    return _review_payload(
        str(path),
        manifest.get("name", path.name) if isinstance(manifest.get("name", path.name), str) else path.name,
        manifest.get("version", "local") if isinstance(manifest.get("version", "local"), str) else "local",
        files,
        contents,
        warnings,
        artifact_hash.hexdigest(),
    )


def _parse_github(source: str) -> tuple[str, str]:
    value = source[4:] if source.startswith("git:") else source
    if value.startswith("github.com/"):
        value = "https://" + value
    if not value.startswith("https://github.com/"):
        raise HubError("Only HTTPS GitHub sources are supported for static git review")
    base, separator, commit = value.rpartition("@")
    if not separator:
        base, commit = value, "HEAD"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", commit) or ".." in commit:
        raise HubError("Use a GitHub URL with an optional branch, tag, or commit after @")
    parsed = urlparse(base)
    if parsed.hostname != "github.com" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HubError("Invalid GitHub source")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        raise HubError("GitHub source must identify one owner/repository")
    url = f"https://github.com/{parts[0]}/{parts[1].removesuffix('.git')}.git"
    return url, commit.lower() if GIT_COMMIT_RE.fullmatch(commit) else commit


def _child_pids() -> set[int]:
    return {int(pid) for pid in Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()}


def _cleanup_command(process: subprocess.Popen, existing_children: set[int]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    deadline = time.monotonic() + 2
    while True:
        # Killing parents adopts their children through the subreaper, including
        # descendants that created a new session. Never touch preexisting children.
        children = _child_pids() - existing_children
        for pid in children:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            if pid == process.pid:
                process.poll()
            else:
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(pid, os.WNOHANG)
        if not (_child_pids() - existing_children):
            process.poll()
            return
        if time.monotonic() >= deadline:
            raise HubError("Helper cleanup did not finish within 2 seconds")
        time.sleep(0.01)


def _run(argv: list[str], cwd: Path, timeout: int, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if Path(argv[0]).name == "git":
        # A repository's attributes must not activate the user's checkout filters.
        env = {key: value for key, value in env.items() if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ATTR_NOSYSTEM": "1"})
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=5",
        "npm_config_ignore_scripts": "true",
        "NPM_CONFIG_IGNORE_SCRIPTS": "true",
    })
    # The backend is a single-request Linux process. Adopt orphaned grandchildren
    # so cleanup can reap the whole command group, including npm/git helpers.
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) or libc.prctl(36, 1, 0, 0, 0):
        raise HubError("Could not enable helper process cleanup")
    process = None
    existing_children = _child_pids()
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        output = {"stdout": bytearray(), "stderr": bytearray()}
        pending = memoryview(input_text.encode() if input_text is not None else b"")
        deadline = time.monotonic() + timeout
        total = 0
        with selectors.DefaultSelector() as selector:
            for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            if process.stdin is not None:
                if pending:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HubError(f"Command timed out after {timeout} seconds")
                for key, _ in selector.select(min(remaining, 0.1)):
                    if key.data == "stdin":
                        try:
                            pending = pending[os.write(key.fd, pending[:65536]):]
                        except BrokenPipeError:
                            pending = pending[:0]
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    chunk = os.read(key.fd, min(65536, MAX_COMMAND_OUTPUT - total + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    total += len(chunk)
                    if total > MAX_COMMAND_OUTPUT:
                        raise HubError("Command output exceeds the 1 MB limit")
                    output[key.data].extend(chunk)
        return subprocess.CompletedProcess(argv, process.wait(), output["stdout"].decode(errors="replace"), output["stderr"].decode(errors="replace"))
    except OSError as exc:
        raise HubError(f"Could not run {argv[0]}: {exc}") from exc
    finally:
        try:
            if process is not None:
                _cleanup_command(process, existing_children)
        finally:
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            libc.prctl(36, previous.value, 0, 0, 0)


class _LimitedReader:
    """Cap bytes before a consumer (including tar's PAX parser) allocates them."""

    def __init__(self, stream, limit: int, label: str):
        self.stream, self.remaining, self.label = stream, limit, label

    def read(self, size: int = -1) -> bytes:
        size = self.remaining + 1 if size < 0 else min(size, self.remaining + 1)
        data = self.stream.read(size)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise HubError(f"GitHub review exceeds the {self.label} limit")
        return data


@contextlib.contextmanager
def _github_deadline():
    # SIGALRM also interrupts blocked DNS/TLS/reads; per-socket timeouts alone
    # permit an endless slow response. This backend runs on the main thread.
    def expired(_signum, _frame):
        raise HubError(f"GitHub review timed out after {GITHUB_REVIEW_TIMEOUT} seconds")
    previous = signal.signal(signal.SIGALRM, expired)
    timer = signal.setitimer(signal.ITIMER_REAL, GITHUB_REVIEW_TIMEOUT)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *timer)
        signal.signal(signal.SIGALRM, previous)


class _GithubDownload:
    def __init__(self):
        self.remaining = MAX_ARCHIVE

    def fetch(self, url: str, accept: str = "application/vnd.github+json") -> bytes:
        request = Request(url, headers={"User-Agent": "omarchy-pi-hub/0.3.1", "Accept": accept,
                                        "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urlopen(request, timeout=8) as response:
                reader = _LimitedReader(response, self.remaining, "10 MB remote response")
                data = bytearray()
                while chunk := reader.read(65536):
                    data.extend(chunk)
                self.remaining = reader.remaining
                return bytes(data)
        except HubError:
            raise
        except OSError as exc:
            raise HubError(f"GitHub download failed (public repositories only; API rate limits apply): {exc}") from exc

    def json(self, url: str) -> dict[str, Any]:
        try:
            value = json.loads(self.fetch(url))
        except (ValueError, UnicodeDecodeError) as exc:
            raise HubError("GitHub returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise HubError("GitHub returned an unexpected response")
        return value


def _github_tree(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = value.get("tree")
    if value.get("truncated") is not False or not isinstance(entries, list) or len(entries) > MAX_REVIEW_FILES:
        raise HubError("GitHub tree is incomplete or exceeds the 500-entry review limit")
    files = {}
    seen = set()
    total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise HubError("Invalid GitHub tree entry")
        name = entry.get("path")
        if (not isinstance(name, str) or not name or len(name) > 4096 or "\0" in name
                or any(part in ("", ".", "..", ".git") for part in name.split("/")) or name in seen):
            raise HubError("Unsafe or duplicate GitHub tree path")
        seen.add(name)
        if entry.get("type") == "tree" and entry.get("mode") == "040000":
            continue
        size = entry.get("size")
        if entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755"):
            raise HubError("GitHub links, submodules, and non-regular files are unsupported")
        if type(size) is not int or size < 0 or not isinstance(entry.get("sha"), str) or not GIT_COMMIT_RE.fullmatch(entry["sha"]):
            raise HubError("Invalid GitHub blob metadata")
        total += size
        if total > MAX_ARCHIVE_UNCOMPRESSED:
            raise HubError("GitHub tree exceeds the 100 MB review limit")
        if Path(name).name == ".gitattributes" and size > MAX_PREVIEW_FILE:
            raise HubError("GitHub attributes are too large to check for checkout filters")
        files[name] = entry
    return files


def _inspect_github_archive(data: bytes, files: dict[str, dict[str, Any]]) -> tuple[list[str], dict[str, str], list[str]]:
    contents, warnings, previews = {}, [], {}
    seen = set()
    members = set()
    prefix = None
    # Bound the decompressed stream itself, including metadata and padding.
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
        limited = _LimitedReader(compressed, MAX_ARCHIVE_UNCOMPRESSED, "100 MB expanded archive")
        with tarfile.open(fileobj=limited, mode="r|") as archive:
            for member in archive:
                if len(members) >= MAX_REVIEW_FILES + 1:
                    raise HubError("GitHub archive exceeds the 500-entry review limit")
                parts = member.name.rstrip("/").split("/")
                if any(part in ("", ".", "..", ".git") or "\0" in part for part in parts) or len(member.name) > 4096:
                    raise HubError("Unsafe GitHub archive path")
                if prefix is None:
                    prefix = parts[0]
                if parts[0] != prefix or member.name in members:
                    raise HubError("Duplicate or inconsistent GitHub archive path")
                members.add(member.name)
                name = "/".join(parts[1:])
                if member.isdir():
                    continue
                expected = files.get(name)
                if not member.isfile() or member.sparse is not None or expected is None or member.size != expected["size"] or name in seen:
                    raise HubError("GitHub archive does not match the pinned tree")
                seen.add(name)
                digest = hashlib.sha1(f"blob {member.size}\0".encode())
                preview = bytearray()
                stream = archive.extractfile(member)
                if stream is None:
                    raise HubError("GitHub archive file is unreadable")
                with stream:
                    while chunk := stream.read(65536):
                        digest.update(chunk)
                        preview.extend(chunk[:max(0, MAX_PREVIEW_FILE - len(preview))])
                if digest.hexdigest() != expected["sha"].lower():
                    raise HubError("GitHub archive content differs from the pinned tree (export transforms or LFS are unsupported)")
                if Path(name).name == ".gitattributes" and b"filter" in preview:
                    raise HubError("Git checkout filters (including LFS) are unsupported: installed content may differ from review")
                if _previewable(name):
                    if member.size > MAX_FILE:
                        warnings.append(f"Large file omitted from preview: {name}")
                    else:
                        previews[name] = bytes(preview)
            # tarfile accepts physical EOF without end blocks. Require both
            # blocks and drain its buffered stream to check padding and gzip CRC.
            if archive.fileobj.tell() != archive.offset + 512 or archive.fileobj.read(512) != b"\0" * 512:
                raise HubError("GitHub archive has no complete tar end marker")
            while chunk := archive.fileobj.read(65536):
                if chunk.strip(b"\0"):
                    raise HubError("GitHub archive has unexpected trailing data")
    if seen != set(files):
        raise HubError("GitHub archive omits pinned files (export-ignore is unsupported)")
    remaining = MAX_REVIEW_TEXT
    for name in sorted(previews, key=_preview_priority):
        if remaining <= 0:
            warnings.append("Some text files have no preview because the 500 KB review limit was reached.")
            break
        contents[name] = _text_preview(previews[name], remaining)
        remaining -= len(contents[name])
        if files[name]["size"] > MAX_PREVIEW_FILE:
            warnings.append(f"Preview truncated to 48 KB: {name}")
    return sorted(files), contents, warnings


def _review_github(source: str) -> dict[str, Any]:
    url, commit = _parse_github(source)
    repository = urlparse(url).path.strip("/").removesuffix(".git")
    api = f"https://api.github.com/repos/{repository}"
    with _github_deadline():
        download = _GithubDownload()
        if not GIT_COMMIT_RE.fullmatch(commit):
            resolved = download.fetch(f"{api}/commits/{quote(commit, safe='')}", "application/vnd.github.sha").decode("ascii").strip()
            if not GIT_COMMIT_RE.fullmatch(resolved):
                raise HubError("Could not resolve that GitHub branch or tag. Check the URL and repository access.")
            commit = resolved.lower()
        tree = _github_tree(download.json(f"{api}/git/trees/{commit}?recursive=1"))
        data = download.fetch(f"https://codeload.github.com/{repository}/tar.gz/{commit}", "application/gzip")
        try:
            files, contents, warnings = _inspect_github_archive(data, tree)
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise HubError("GitHub returned an invalid archive") from exc
        manifest: dict[str, Any] = {}
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(contents.get("package.json", "{}"))
            if isinstance(parsed, dict):
                manifest = parsed
        exact = f"git:{url}@{commit}"
        return _review_payload(exact, manifest.get("name", url.rsplit("/", 1)[-1].removesuffix(".git")), manifest.get("version", commit[:12]), files, contents, warnings, commit)


def _store_review(review: dict[str, Any], scope: dict[str, Any]) -> str:
    token = secrets.token_urlsafe(24)
    record = {
        "source": review["source"],
        "version": review["version"],
        "digest": review["_digest"],
        "scope": scope["key"],
        "expires": int(time.time()) + REVIEW_TTL,
    }
    def change(state: dict[str, Any]) -> None:
        reviews = state.setdefault("reviews", {})
        if not isinstance(reviews, dict):
            state["reviews"] = reviews = {}
        now = int(time.time())
        for old_token in list(reviews):
            old = reviews.get(old_token)
            if not isinstance(old, dict) or old.get("expires", 0) < now:
                reviews.pop(old_token, None)
        reviews[token] = record
    _state_update(change)
    return token


def action_review(request: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    source = request.get("source")
    if not isinstance(source, str) or not source:
        raise HubError("source is required")
    if request.get("action") == "reviewUpdate" and source.startswith("npm:"):
        name, _ = _split_npm(source)
        source = "npm:" + name
    elif request.get("action") == "reviewUpdate" and source.startswith(("git:", "https://github.com/")):
        source, _ = _parse_github(source)
    elif source.startswith(("./", "../")):
        packages = _settings(scope).get("packages", [])
        if isinstance(packages, list) and any(_package_source(entry) == source for entry in packages):
            source = str((scope["settings"].parent / source).resolve())
    if source.startswith("npm:"):
        review = _review_npm(source)
    elif source.startswith(("git:", "https://github.com/")):
        review = _review_github(source)
    else:
        review = _review_local(source, scope)
    token = _store_review(review, scope)
    review = dict(review)
    review.pop("_digest", None)
    review["token"] = token
    return {"review": review}


def _pi_path() -> str | None:
    override = os.environ.get("PI_HUB_PI")
    if override:
        return override if Path(override).is_file() and os.access(override, os.X_OK) else None
    found = shutil.which("pi")
    if found:
        return found
    known = _home() / ".local" / "share" / "mise" / "installs" / "pi" / "latest" / "pi" / "pi"
    return str(known) if known.is_file() and os.access(known, os.X_OK) else None


def action_install(request: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    source, token = request.get("source"), request.get("token")
    if not isinstance(source, str) or not isinstance(token, str):
        raise HubError("source and review token are required")
    state = _json_read(_state_file(), {})
    reviews = state.get("reviews", {}) if isinstance(state, dict) else {}
    record = reviews.get(token) if isinstance(reviews, dict) else None
    if not isinstance(record, dict) or record.get("expires", 0) < int(time.time()):
        raise HubError("Review token is invalid or expired; review the package again")
    if record.get("source") != source or record.get("scope") != scope["key"] or not record.get("digest"):
        raise HubError("Review token does not match this exact source and scope")
    if source.startswith("npm:"):
        current_review = _review_npm(source)
    elif source.startswith(("git:", "https://github.com/")):
        current_review = _review_github(source)
    else:
        current_review = _review_local(source, scope)
    if not secrets.compare_digest(str(record["digest"]), current_review["_digest"]):
        raise HubError("Package content changed after review; review it again before installing")
    pi = _pi_path()
    if pi is None:
        raise HubError("Pi executable is unavailable")
    argv = [pi, "install", source]
    if scope["scope"] == "project":
        argv.extend(["-l", "--approve"])
    else:
        argv.append("--no-approve")
    completed = _run(argv, scope["project"], 180)
    if completed.returncode:
        raise HubError(completed.stderr.strip() or completed.stdout.strip() or "Pi install failed")
    def consume(state_value: dict[str, Any]) -> None:
        stored = state_value.get("reviews")
        if isinstance(stored, dict):
            stored.pop(token, None)
    _state_update(consume)
    return {"message": completed.stdout.strip() or f"Installed {source}"}


def _entry_index(packages: list[Any], source: str) -> int:
    for index, entry in enumerate(packages):
        if _package_source(entry) == source:
            return index
    raise HubError("Package is not installed in the selected scope")


def _toggle_package(scope: dict[str, Any], source: str, enabled: bool) -> str:
    state_key = scope["key"] + "\0" + source
    if not enabled:
        current_settings = _settings(scope)
        current_packages = current_settings.get("packages", [])
        if not isinstance(current_packages, list):
            raise HubError("packages in Pi settings must be an array")
        original_entry = current_packages[_entry_index(current_packages, source)]
        def save_original(state: dict[str, Any]) -> None:
            disabled = state.setdefault("disabledPackages", {})
            if not isinstance(disabled, dict):
                state["disabledPackages"] = disabled = {}
            disabled.setdefault(state_key, original_entry)
        _state_update(save_original)
        def disable(settings: dict[str, Any]) -> None:
            packages = settings.get("packages", [])
            if not isinstance(packages, list):
                raise HubError("packages in Pi settings must be an array")
            index = _entry_index(packages, source)
            current = dict(packages[index]) if isinstance(packages[index], dict) else {"source": source}
            for kind in PACKAGE_KINDS:
                current[kind] = []
            packages[index] = current
        _settings_update(scope, disable)
        return f"Disabled {source}"

    state = _json_read(_state_file(), {})
    disabled = state.get("disabledPackages", {}) if isinstance(state, dict) else {}
    restored_entry = disabled.get(state_key) if isinstance(disabled, dict) else None
    def enable(settings: dict[str, Any]) -> None:
        packages = settings.get("packages", [])
        if not isinstance(packages, list):
            raise HubError("packages in Pi settings must be an array")
        index = _entry_index(packages, source)
        if restored_entry is not None:
            packages[index] = restored_entry
        elif isinstance(packages[index], dict):
            current = dict(packages[index])
            for kind in PACKAGE_KINDS:
                if current.get(kind) == []:
                    current.pop(kind, None)
            packages[index] = current
    _settings_update(scope, enable)
    def forget_original(state_value: dict[str, Any]) -> None:
        stored = state_value.get("disabledPackages", {})
        if isinstance(stored, dict):
            stored.pop(state_key, None)
    _state_update(forget_original)
    return f"Enabled {source}"


def _toggle_resource(scope: dict[str, Any], request: dict[str, Any]) -> str:
    path_value, enabled = request.get("path"), request.get("enabled")
    if not isinstance(path_value, str) or not os.path.isabs(path_value) or not isinstance(enabled, bool):
        raise HubError("toggleResource needs an absolute path and boolean enabled")
    found = {item["path"]: item for item in action_list(request, scope)["resources"]}
    item = found.get(str(Path(path_value).resolve()))
    if item is None:
        raise HubError("Resource is not part of the selected Pi scope")
    kind, exact = item["kind"], str(Path(path_value).resolve())
    state_key = scope["key"] + "\0" + kind + "\0" + exact
    if not enabled:
        current_settings = _settings(scope)
        current_values = current_settings.get(kind, [])
        if not isinstance(current_values, list):
            raise HubError(f"{kind} in Pi settings must be an array")
        variants = (exact, "+" + exact, "-" + exact, "!" + exact)
        prior = [[index, value] for index, value in enumerate(current_values) if value in variants]
        def remember(state: dict[str, Any]) -> None:
            toggles = state.setdefault("disabledResources", {})
            if not isinstance(toggles, dict):
                state["disabledResources"] = toggles = {}
            toggles.setdefault(state_key, prior)
        _state_update(remember)
        def disable(settings: dict[str, Any]) -> None:
            values = settings.get(kind, [])
            if not isinstance(values, list):
                raise HubError(f"{kind} in Pi settings must be an array")
            values = [v for v in values if v not in variants]
            values.append("-" + exact)
            settings[kind] = values
        _settings_update(scope, disable)
        return f"Disabled {item['name']}"
    state = _json_read(_state_file(), {})
    toggles = state.get("disabledResources", {}) if isinstance(state, dict) else {}
    prior = toggles.get(state_key) if isinstance(toggles, dict) else None
    def enable(settings: dict[str, Any]) -> None:
        values = settings.get(kind, [])
        if not isinstance(values, list):
            raise HubError(f"{kind} in Pi settings must be an array")
        variants = (exact, "+" + exact, "-" + exact, "!" + exact)
        values = [v for v in values if v not in variants]
        if isinstance(prior, list):
            for saved in prior:
                if isinstance(saved, list) and len(saved) == 2 and isinstance(saved[0], int) and isinstance(saved[1], str):
                    values.insert(min(saved[0], len(values)), saved[1])
            settings[kind] = values
        else:
            if "+" + exact not in values and exact not in values:
                values.append("+" + exact)
            settings[kind] = values
    _settings_update(scope, enable)
    def forget(state_value: dict[str, Any]) -> None:
        stored = state_value.get("disabledResources", {})
        if isinstance(stored, dict):
            stored.pop(state_key, None)
    _state_update(forget)
    return f"Enabled {item['name']}"


def action_installed(request: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    operation = request.get("action")
    if operation == "toggleResource":
        return {"message": _toggle_resource(scope, request)}
    source = request.get("source")
    if not isinstance(source, str) or not source:
        raise HubError("source is required")
    if operation == "toggle":
        enabled = request.get("enabled")
        if not isinstance(enabled, bool):
            raise HubError("enabled must be boolean")
        return {"message": _toggle_package(scope, source, enabled)}
    if operation == "remove":
        if request.get("confirm") is not True:
            raise HubError("Package removal requires confirm:true")
        pi = _pi_path()
        if pi is None:
            raise HubError("Pi executable is unavailable")
        packages = _settings(scope).get("packages", [])
        if not isinstance(packages, list):
            raise HubError("packages in Pi settings must be an array")
        _entry_index(packages, source)
        command_source = str((scope["settings"].parent / source).resolve()) if source.startswith(("./", "../")) else source
        argv = [pi, "remove", command_source]
        if scope["scope"] == "project":
            argv.extend(["-l", "--approve"])
        else:
            argv.append("--no-approve")
        completed = _run(argv, scope["project"], 90)
        if completed.returncode:
            raise HubError(completed.stderr.strip() or completed.stdout.strip() or "Pi remove failed")
        return {"message": completed.stdout.strip() or f"Removed {source}"}
    raise HubError("installed action must be remove, toggle, or toggleResource")


def _all_resources(request: dict[str, Any], scope: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["path"]: item for item in action_list(request, scope)["resources"]}


def action_read(request: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    value = request.get("path")
    if not isinstance(value, str) or not os.path.isabs(value):
        raise HubError("path must be absolute")
    path = Path(value).resolve()
    item = _all_resources(request, scope).get(str(path))
    if item is None:
        raise HubError("Resource is not part of the selected Pi scope")
    body = _read_text(path)
    result = dict(item)
    result.update({"body": body, "revision": _revision(body)})
    return {"resource": result}


def _owned_root(scope: dict[str, Any], kind: str) -> Path:
    if kind not in RESOURCE_KINDS:
        raise HubError("kind must be extensions, skills, or prompts")
    return scope["agent_dir"] / kind


def _new_path(scope: dict[str, Any], kind: str, name: str) -> Path:
    root = _owned_root(scope, kind)
    if kind == "skills":
        return root / name / "SKILL.md"
    return root / f"{name}{'.ts' if kind == 'extensions' else '.md'}"


def _validate_body(kind: str, name: str, body: Any) -> None:
    if not isinstance(body, str):
        raise HubError("body must be a string")
    if len(body.encode("utf-8")) > MAX_FILE:
        raise HubError("Resource is too large")
    if kind == "skills":
        skill_name, _ = _validate_skill(body)
        if skill_name != name:
            raise HubError("Skill frontmatter name must match the requested name")
    else:
        _validate_name(name)


def _backup(path: Path, scope: dict[str, Any]) -> None:
    if not path.exists():
        return
    backup_dir = _state_file().parent / "backups" / hashlib.sha256(scope["key"].encode()).hexdigest()[:12]
    _assert_no_symlink(backup_dir, _state_file().parent)
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"{int(time.time() * 1000)}-{secrets.token_hex(6)}-{path.name}"
    shutil.copy2(path, destination, follow_symlinks=False)


def action_save(request: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    kind = request.get("kind")
    if kind not in RESOURCE_KINDS:
        raise HubError("kind must be extensions, skills, or prompts")
    name = _validate_name(request.get("name"))
    body = request.get("body")
    _validate_body(kind, name, body)
    value = request.get("path", "")
    if not isinstance(value, str):
        raise HubError("path must be a string")
    root = _owned_root(scope, kind)
    if value:
        if not os.path.isabs(value):
            raise HubError("Existing resource path must be absolute")
        path = Path(value)
        expected = request.get("revision")
        if not isinstance(expected, str) or not expected:
            raise HubError("Existing resource save requires its revision")
        _assert_no_symlink(path, root)
        if not path.is_file():
            raise HubError("Resource no longer exists")
        current = _read_text(path)
        if not secrets.compare_digest(_revision(current), expected):
            raise HubError("Resource changed since it was opened; reload before saving")
        if kind == "skills" and path.name != "SKILL.md" and path.suffix != ".md":
            raise HubError("Skill resources must be Markdown files")
        if kind == "prompts" and path.suffix != ".md":
            raise HubError("Prompt templates must be Markdown files")
        if kind == "extensions" and path.suffix not in (".ts", ".js"):
            raise HubError("Extensions must use .ts or .js")
        message = f"Saved {name}"
    else:
        path = _new_path(scope, kind, name)
        _assert_no_symlink(path, root)
        if path.exists():
            raise HubError("A resource with this name already exists")
        message = f"Created {name}"
    with _file_lock(path):
        # Recheck inside the lock so two editors cannot pass the revision check.
        if value:
            current = _read_text(path)
            if not secrets.compare_digest(_revision(current), request["revision"]):
                raise HubError("Resource changed since it was opened; reload before saving")
            _backup(path, scope)
        elif path.exists():
            raise HubError("A resource with this name already exists")
        _atomic_write(path, body.encode("utf-8"), 0o600)
    saved = _resource(kind, path, True, _resource_enabled(_settings(scope), kind, path))
    saved.update({"body": body, "revision": _revision(body)})
    return {"resource": saved, "message": message}


def action_delete(request: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    if request.get("confirm") is not True:
        raise HubError("Delete requires confirm:true")
    value, expected = request.get("path"), request.get("revision")
    if not isinstance(value, str) or not os.path.isabs(value) or not isinstance(expected, str):
        raise HubError("Delete requires an absolute path and revision")
    path = Path(value)
    resources = _all_resources(request, scope)
    item = resources.get(str(path.resolve()))
    if item is None or not item["editable"]:
        raise HubError("Only resources owned by the selected Pi scope can be deleted")
    root = _owned_root(scope, item["kind"])
    _assert_no_symlink(path, root)
    current = _read_text(path)
    if not secrets.compare_digest(_revision(current), expected):
        raise HubError("Resource changed since it was opened; reload before deleting")
    target = path.parent if item["kind"] == "skills" and path.name == "SKILL.md" and path.parent != root else path
    _assert_no_symlink(target, root)
    token = secrets.token_urlsafe(24)
    trash_root = _state_file().parent / "trash" / token
    _assert_no_symlink(trash_root, _state_file().parent)
    trash_root.mkdir(parents=True, exist_ok=False)
    trashed = trash_root / target.name
    shutil.move(str(target), str(trashed))
    record = {"scope": scope["key"], "original": str(target), "trashed": str(trashed), "created": int(time.time())}
    def remember(state: dict[str, Any]) -> None:
        trash = state.setdefault("trash", {})
        if not isinstance(trash, dict):
            state["trash"] = trash = {}
        trash[token] = record
    _state_update(remember)
    return {"message": f"Deleted {item['name']}; it can be restored.", "undoToken": token}


def action_restore(request: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    token = request.get("token")
    if not isinstance(token, str) or not token:
        raise HubError("Restore token is required")
    state = _json_read(_state_file(), {})
    trash = state.get("trash", {}) if isinstance(state, dict) else {}
    record = trash.get(token) if isinstance(trash, dict) else None
    if not isinstance(record, dict) or record.get("scope") != scope["key"]:
        raise HubError("Restore token is invalid for this scope")
    original = Path(record.get("original", ""))
    trashed = Path(record.get("trashed", ""))
    _assert_no_symlink(trashed, _state_file().parent / "trash")
    if not trashed.exists():
        raise HubError("Trashed resource is no longer available")
    if original.exists():
        raise HubError("Cannot restore because the original path is occupied")
    # Original must remain inside one of the writable roots.
    matched = False
    for kind in RESOURCE_KINDS:
        try:
            _assert_no_symlink(original, _owned_root(scope, kind))
            matched = True
            break
        except HubError:
            continue
    if not matched:
        raise HubError("Restore destination is outside the selected Pi scope")
    original.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(trashed), str(original))
    def forget(state_value: dict[str, Any]) -> None:
        stored = state_value.get("trash", {})
        if isinstance(stored, dict):
            stored.pop(token, None)
    _state_update(forget)
    with contextlib.suppress(OSError):
        trashed.parent.rmdir()
    return {"message": f"Restored {original.name}"}


def action_scaffold(request: dict[str, Any], _scope_data: dict[str, Any]) -> dict[str, Any]:
    kind, template = request.get("kind"), request.get("template")
    name = _validate_name(request.get("name"))
    description = request.get("description", "")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise HubError("description must be a non-empty string up to 1024 characters")
    if (kind, template) == ("skills", "skill"):
        body = f"---\nname: {name}\ndescription: {description.strip()}\n---\n\n# {name.replace('-', ' ').title()}\n\nDescribe the workflow and any constraints here.\n"
    elif (kind, template) == ("prompts", "prompt"):
        body = f"---\ndescription: {description.strip()}\n---\n\nWrite the prompt instructions here.\n"
    elif kind == "extensions" and template in ("command", "tool", "guard"):
        if template == "command":
            body = f"export default function (pi) {{\n  pi.registerCommand(\"{name}\", {{\n    description: {json.dumps(description.strip())},\n    handler: async (args, ctx) => {{\n      ctx.ui.notify(`{name}: ${{args}}`, \"info\");\n    }},\n  }});\n}}\n"
        elif template == "tool":
            body = f"import {{ Type }} from \"typebox\";\n\nexport default function (pi) {{\n  pi.registerTool({{\n    name: \"{name}\",\n    label: \"{name.replace('-', ' ').title()}\",\n    description: {json.dumps(description.strip())},\n    parameters: Type.Object({{ input: Type.String() }}),\n    execute: async (_toolCallId, params) => ({{ content: [{{ type: \"text\", text: params.input }}], details: {{}} }}),\n  }});\n}}\n"
        else:
            body = f"import type {{ ExtensionAPI }} from \"@earendil-works/pi-coding-agent\";\n\nexport default function (pi: ExtensionAPI) {{\n  // This confirmation gate is a convenience, not a complete command sandbox.\n  pi.on(\"tool_call\", async (event, ctx) => {{\n    if (event.toolName !== \"bash\") return undefined;\n    const command = String(event.input.command ?? \"\");\n    if (!ctx.hasUI) return {{ block: true, reason: \"{name} requires interactive approval for bash commands\" }};\n    const allowed = await ctx.ui.confirm(\"Allow bash command?\", command);\n    return allowed ? undefined : {{ block: true, reason: \"Bash command declined by user\" }};\n  }});\n}}\n"
    else:
        raise HubError("Template does not match the requested resource kind")
    return {"body": body}


HANDLERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = {
    "list": action_list,
    "search": action_search,
    "review": action_review,
    "reviewUpdate": action_review,
    "install": action_install,
    "installed": action_installed,
    "toggle": action_installed,
    "toggleResource": action_installed,
    "remove": action_installed,
    "read": action_read,
    "save": action_save,
    "delete": action_delete,
    "restore": action_restore,
    "scaffold": action_scaffold,
}


def handle(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise HubError("Request must be a JSON object")
    scope = _scope(request)
    action = request.get("action")
    if action not in HANDLERS:
        raise HubError("Unknown action")
    return {"ok": True, **HANDLERS[action](request, scope)}


def rpc() -> int:
    try:
        request = json.load(sys.stdin)
        response = handle(request)
    except HubError as exc:
        response = {"ok": False, "error": str(exc)}
    except (json.JSONDecodeError, UnicodeDecodeError):
        response = {"ok": False, "error": "stdin must contain one valid JSON request"}
    except Exception as exc:  # Keep the UI protocol intact without leaking a traceback.
        response = {"ok": False, "error": f"Unexpected backend error: {exc}"}
    json.dump(response, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


def main(argv: list[str]) -> int:
    if argv != ["rpc"]:
        print("usage: python3 hub.py rpc", file=sys.stderr)
        return 2
    return rpc()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
