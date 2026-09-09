"""Hostile acquisition fixtures and real command-group cleanup regressions."""
import gzip
import io
import json
import os
from pathlib import Path
import signal
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

from test_hub import hub


def fixture(entries):
    tree = []
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in entries.items():
            digest = hub.hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
            tree.append(dict(path=name, type="blob", mode="100644", size=len(data), sha=digest))
            info = tarfile.TarInfo("demo/" + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return {"truncated": False, "tree": tree}, buffer.getvalue()


class AcquisitionTest(unittest.TestCase):
    def test_remote_budget_is_shared_and_stops_reading_without_content_length(self):
        class Response(io.BytesIO):
            consumed = 0
            def read(self, size=-1):
                data = super().read(size)
                self.consumed += len(data)
                return data
        first, second = Response(b"a" * 40), Response(b"b" * 10000)
        with mock.patch.object(hub, "MAX_ARCHIVE", 64), mock.patch.object(hub, "urlopen", side_effect=[first, second]):
            download = hub._GithubDownload()
            self.assertEqual(len(download.fetch("https://api.github.com/first")), 40)
            with self.assertRaisesRegex(hub.HubError, "remote response"):
                download.fetch("https://api.github.com/second")
        self.assertEqual(first.consumed + second.consumed, 65)

    def test_oversized_tree_rejected_before_archive_download_or_token(self):
        tree, _ = fixture({"README.md": b"demo"})
        for value in ({**tree, "truncated": True}, {**tree, "tree": tree["tree"] * 501},
                      {**tree, "tree": [{**tree["tree"][0], "size": hub.MAX_ARCHIVE_UNCOMPRESSED + 1}]}):
            with self.subTest(value=str(value)[:100]), mock.patch.object(hub, "urlopen", return_value=io.BytesIO(json.dumps(value).encode())) as opened:
                with self.assertRaises(hub.HubError):
                    hub._review_github("https://github.com/example/demo@" + "a" * 40)
                self.assertEqual(opened.call_count, 1)

    def test_tree_rejects_links_submodules_paths_duplicates_and_large_attributes(self):
        tree, _ = fixture({"README": b"demo"})
        entry = tree["tree"][0]
        changes = [{"mode": "120000"}, {"mode": "160000", "type": "commit"},
                   {"path": "../escape"}, {"path": "/absolute"}, {"path": "a//b"},
                   {"path": ".git/config"}, {"size": -1}, {"sha": "bad"},
                   {"path": ".gitattributes", "size": hub.MAX_PREVIEW_FILE + 1}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(hub.HubError):
                hub._github_tree({"truncated": False, "tree": [{**entry, **change}]})
        with self.assertRaises(hub.HubError):
            hub._github_tree({"truncated": False, "tree": [entry, entry]})

    def test_git_filters_are_rejected_in_nested_attributes(self):
        tree, archive = fixture({"nested/.gitattributes": b"*.ts filter=lfs\n", "README": b"demo"})
        with self.assertRaisesRegex(hub.HubError, "checkout filters"):
            hub._inspect_github_archive(archive, hub._github_tree(tree))

    def test_archive_omissions_and_transforms_are_rejected(self):
        tree, _ = fixture({"README": b"original", "hidden.ts": b"secret"})
        for entries in ({"README": b"original"}, {"README": b"modified", "hidden.ts": b"secret"}):
            _, archive = fixture(entries)
            with self.subTest(entries=entries), self.assertRaises(hub.HubError):
                hub._inspect_github_archive(archive, hub._github_tree(tree))

    def test_archive_expansion_and_pax_metadata_are_bounded(self):
        tree, archive = fixture({"data.bin": b"x" * 100000})
        with mock.patch.object(hub, "MAX_ARCHIVE_UNCOMPRESSED", 20000), self.assertRaisesRegex(hub.HubError, "expanded archive"):
            hub._inspect_github_archive(archive, {x["path"]: x for x in tree["tree"]})
        # Huge metadata is consumed by tarfile before it yields a TarInfo.
        header = tarfile.TarInfo("pax")
        header.type = tarfile.XHDTYPE
        header.size = 10000000
        archive = gzip.compress(header.tobuf() + b"x" * 100000)
        with mock.patch.object(hub, "MAX_ARCHIVE_UNCOMPRESSED", 20000), self.assertRaisesRegex(hub.HubError, "expanded archive"):
            hub._inspect_github_archive(archive, {})

    def test_archive_trailing_expansion_is_bounded(self):
        tree, archive = fixture({"README": b"demo"})
        archive += gzip.compress(b"\0" * 100000)
        with mock.patch.object(hub, "MAX_ARCHIVE_UNCOMPRESSED", 20000), self.assertRaisesRegex(hub.HubError, "expanded archive"):
            hub._inspect_github_archive(archive, hub._github_tree(tree))

    def test_archive_requires_complete_end_markers_and_gzip_trailer(self):
        tree, archive = fixture({"README": b"demo"})
        raw = gzip.decompress(archive)
        for invalid in (gzip.compress(raw[:1024]), gzip.compress(raw[:1536]),
                        gzip.compress(raw + b"unexpected"), archive[:-8]):
            with self.subTest(size=len(invalid)), self.assertRaises((hub.HubError, EOFError)):
                hub._inspect_github_archive(invalid, hub._github_tree(tree))

    def test_archive_rejects_unsafe_and_nonregular_members(self):
        tree, _ = fixture({"README": b"demo"})
        for name, kind in (("demo/../escape", tarfile.REGTYPE), ("demo/README", tarfile.SYMTYPE),
                           ("demo/README", tarfile.LNKTYPE), ("demo/README", tarfile.FIFOTYPE)):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                info = tarfile.TarInfo(name)
                info.type = kind
                archive.addfile(info)
            with self.subTest(name=name, kind=kind), self.assertRaises(hub.HubError):
                hub._inspect_github_archive(buffer.getvalue(), hub._github_tree(tree))

    def test_review_deadline_interrupts_blocked_read_and_restores_handler(self):
        class SlowResponse(io.BytesIO):
            def read(self, size=-1):
                time.sleep(10)
                return b""
        previous = signal.getsignal(signal.SIGALRM)
        started = time.monotonic()
        with mock.patch.object(hub, "GITHUB_REVIEW_TIMEOUT", 0.1), mock.patch.object(hub, "urlopen", return_value=SlowResponse()):
            with self.assertRaisesRegex(hub.HubError, "timed out"):
                hub._review_github("https://github.com/example/demo")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)

    def test_commands_preserve_input_output_and_exit_status(self):
        script = "import sys; s=sys.stdin.read(); print(s); print('diagnostic',file=sys.stderr); sys.exit(3)"
        result = hub._run([sys.executable, "-c", script], Path.cwd(), 5, "x" * 100000)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "x" * 100000 + "\n")
        self.assertEqual(result.stderr, "diagnostic\n")

    def test_output_limit_timeout_and_success_reap_command_descendants(self):
        for mode in ("stdout", "stderr", "timeout", "success", "detached"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                script = """
import os, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path('parent').write_text(str(os.getpid()))
child = os.fork()
if child == 0:
    if MODE == 'detached': os.setsid()
    Path('child').write_text(str(os.getpid()))
    if MODE == 'success':
        os.close(1); os.close(2)
    time.sleep(30)
else:
    while not Path('child').exists(): time.sleep(0.001)
    if MODE in ('stdout', 'stderr'):
        fd = 1 if MODE == 'stdout' else 2
        while True: os.write(fd, b'x' * 4096)
    elif MODE in ('timeout', 'detached'):
        time.sleep(30)
""".replace("MODE", repr(mode))
                started = time.monotonic()
                with mock.patch.object(hub, "MAX_COMMAND_OUTPUT", 16000):
                    if mode == "success":
                        self.assertEqual(hub._run([sys.executable, "-c", script], root, 3).returncode, 0)
                    else:
                        message = "timed out" if mode in ("timeout", "detached") else "output exceeds"
                        with self.assertRaisesRegex(hub.HubError, message):
                            hub._run([sys.executable, "-c", script], root, 0.3 if mode in ("timeout", "detached") else 3)
                self.assertLess(time.monotonic() - started, 5)
                for name in ("parent", "child"):
                    pid = int((root / name).read_text())
                    self.assertFalse(Path(f"/proc/{pid}").exists(), f"{name} {pid} survived cleanup")


if __name__ == "__main__":
    unittest.main()
