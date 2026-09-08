import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pi_hub_backend", ROOT / "hub.py")
hub = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(hub)


class HubTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.project = self.base / "project"
        self.agent = self.home / "custom-pi"
        self.home.mkdir()
        self.project.mkdir()
        self.environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "XDG_STATE_HOME": str(self.home / "state"),
                "PI_CODING_AGENT_DIR": str(self.agent),
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def request(self, action, scope="global", **values):
        return {"action": action, "scope": scope, "project": str(self.project), **values}

    def write_settings(self, value, scope="global"):
        directory = self.agent if scope == "global" else self.project / ".pi"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "settings.json").write_text(json.dumps(value), encoding="utf-8")

    def read_settings(self, scope="global"):
        directory = self.agent if scope == "global" else self.project / ".pi"
        return json.loads((directory / "settings.json").read_text(encoding="utf-8"))

    def test_rpc_rejects_missing_explicit_project(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "hub.py"), "rpc"],
            input=json.dumps({"action": "list", "scope": "global"}),
            text=True,
            capture_output=True,
            env=os.environ.copy(),
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout)["ok"], False)
        self.assertIn("absolute existing folder", completed.stdout)

    def test_resource_round_trip_stale_revision_and_restore(self):
        body = "---\nname: demo-skill\ndescription: Demo workflow\n---\n\n# Demo\n"
        created = hub.handle(self.request("save", kind="skills", name="demo-skill", path="", body=body, revision=""))
        self.assertTrue(created["ok"])
        path = Path(created["resource"]["path"])
        self.assertEqual(path, self.agent / "skills" / "demo-skill" / "SKILL.md")

        listing = hub.handle(self.request("list"))
        listed = next(item for item in listing["resources"] if item["name"] == "demo-skill")
        self.assertTrue(listed["editable"])
        opened = hub.handle(self.request("read", path=str(path)))["resource"]
        self.assertEqual(opened["body"], body)

        path.write_text(body + "external edit\n", encoding="utf-8")
        with self.assertRaisesRegex(hub.HubError, "changed since"):
            hub.handle(self.request("save", kind="skills", name="demo-skill", path=str(path), body=body, revision=opened["revision"]))

        current = hub.handle(self.request("read", path=str(path)))["resource"]
        deleted = hub.handle(self.request("delete", path=str(path), revision=current["revision"], confirm=True))
        self.assertFalse(path.exists())
        restored = hub.handle(self.request("restore", token=deleted["undoToken"]))
        self.assertIn("Restored", restored["message"])
        self.assertEqual(path.read_text(encoding="utf-8"), body + "external edit\n")

    def test_shared_skills_are_browsable_but_read_only(self):
        shared = self.home / ".agents" / "skills" / "shared"
        shared.mkdir(parents=True)
        skill = shared / "SKILL.md"
        skill.write_text("---\nname: shared\ndescription: Shared skill\n---\n", encoding="utf-8")
        listing = hub.handle(self.request("list"))
        item = next(item for item in listing["resources"] if item["name"] == "shared")
        self.assertFalse(item["editable"])
        opened = hub.handle(self.request("read", path=str(skill)))["resource"]
        self.assertIn("Shared skill", opened["body"])
        with self.assertRaisesRegex(hub.HubError, "outside"):
            hub.handle(self.request("save", kind="skills", name="shared", path=str(skill), body=opened["body"], revision=opened["revision"]))

    def test_symlinked_project_settings_are_never_written(self):
        target = self.base / "outside"
        target.mkdir()
        (target / "settings.json").write_text('{"packages":["npm:demo@1.0.0"]}', encoding="utf-8")
        (self.project / ".pi").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(hub.HubError, "symlink"):
            hub.handle(self.request("toggle", scope="project", source="npm:demo@1.0.0", enabled=False))
        self.assertEqual(json.loads((target / "settings.json").read_text())["packages"], ["npm:demo@1.0.0"])

    def test_package_toggle_preserves_config_and_restores_original_entry(self):
        original = {
            "source": "npm:demo@1.2.3",
            "skills": ["skills/review/**"],
            "custom": {"keep": True},
        }
        self.write_settings({"defaultModel": "still-here", "packages": [original]})
        disabled = hub.handle(self.request("toggle", source=original["source"], enabled=False))
        self.assertIn("Disabled", disabled["message"])
        settings = self.read_settings()
        self.assertEqual(settings["defaultModel"], "still-here")
        self.assertTrue(all(settings["packages"][0][kind] == [] for kind in hub.PACKAGE_KINDS))
        self.assertFalse(Path(str(self.agent / "settings.json") + ".lock").exists())

        hub.handle(self.request("toggle", source=original["source"], enabled=True))
        self.assertEqual(self.read_settings()["packages"][0], original)
        backups = list((self.home / "state" / "pi-hub" / "backups").rglob("*settings.json"))
        self.assertGreaterEqual(len(backups), 2)

    def test_installed_relative_local_source_reviews_and_removes_by_absolute_identity(self):
        package = self.base / "package"
        package.mkdir()
        (package / "package.json").write_text('{"name":"relative-demo","version":"1.0.0"}', encoding="utf-8")
        source = "../../package"
        self.write_settings({"packages": [source]}, scope="project")
        review = hub.handle(self.request("review", scope="project", source=source))["review"]
        self.assertEqual(review["source"], str(package))

        completed = subprocess.CompletedProcess([], 0, "removed", "")
        with mock.patch.object(hub, "_pi_path", return_value="/fake/pi"), mock.patch.object(hub, "_run", return_value=completed) as run:
            hub.handle(self.request("remove", scope="project", source=source, confirm=True))
        self.assertEqual(run.call_args.args[0], ["/fake/pi", "remove", str(package), "-l", "--approve"])

    def test_resource_toggle_restores_only_its_own_exact_entries(self):
        prompts = self.agent / "prompts"
        prompts.mkdir(parents=True)
        prompt = prompts / "acceptance.md"
        prompt.write_text("---\ndescription: Test\n---\nPrompt\n", encoding="utf-8")
        exact = str(prompt)
        self.write_settings({"prompts": ["other", "+" + exact]})
        hub.handle(self.request("toggleResource", path=exact, enabled=False))
        disabled = self.read_settings()["prompts"]
        self.assertEqual(disabled, ["other", "-" + exact])

        settings = self.read_settings()
        settings["prompts"].append("concurrent-edit")
        self.write_settings(settings)
        hub.handle(self.request("toggleResource", path=exact, enabled=True))
        self.assertEqual(self.read_settings()["prompts"], ["other", "+" + exact, "concurrent-edit"])

    def test_local_review_install_is_exact_and_rechecks_content(self):
        package = self.base / "package"
        package.mkdir()
        (package / "package.json").write_text(json.dumps({"name": "local-demo", "version": "1.0.0"}), encoding="utf-8")
        (package / "README.md").write_text("Review me", encoding="utf-8")
        (package / "extensions").mkdir()
        (package / "extensions" / "index.ts").write_text("export default () => {};", encoding="utf-8")

        review = hub.handle(self.request("review", source=str(package)))["review"]
        self.assertEqual(review["source"], str(package))
        self.assertEqual(review["readme"], "Review me")
        self.assertIn("extensions/index.ts", review["fileContents"])
        completed = subprocess.CompletedProcess([], 0, "installed", "")
        with mock.patch.object(hub, "_pi_path", return_value="/fake/pi"), mock.patch.object(hub, "_run", return_value=completed) as run:
            result = hub.handle(self.request("install", source=str(package), token=review["token"]))
        self.assertEqual(result["message"], "installed")
        argv, cwd, timeout = run.call_args.args
        self.assertEqual(argv, ["/fake/pi", "install", str(package), "--no-approve"])
        self.assertEqual(cwd, self.project)
        self.assertEqual(timeout, 180)

        second = hub.handle(self.request("review", source=str(package)))["review"]
        (package / "extensions" / "index.ts").write_text("changed", encoding="utf-8")
        with mock.patch.object(hub, "_pi_path", return_value="/fake/pi"), mock.patch.object(hub, "_run") as run:
            with self.assertRaisesRegex(hub.HubError, "changed after review"):
                hub.handle(self.request("install", source=str(package), token=second["token"]))
            run.assert_not_called()

    def test_npm_review_verifies_archive_and_prioritizes_useful_files(self):
        archive_buffer = io.BytesIO()
        with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
            entries = {
                "package/CHANGELOG.md": b"x" * 200_000,
                "package/extensions/index.ts": b"export default () => {};",
                "package/README.md": b"Useful readme",
                "package/package.json": json.dumps({"name": "demo", "version": "1.2.3", "scripts": {"postinstall": "bad"}, "dependencies": {"x": "1"}}).encode(),
            }
            for name, data in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        archive_data = archive_buffer.getvalue()
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(archive_data).digest()).decode()
        metadata = {
            "dist-tags": {"latest": "1.2.3"},
            "versions": {"1.2.3": {"name": "demo", "version": "1.2.3", "dist": {"tarball": "https://registry.example/demo.tgz", "integrity": integrity}}},
        }
        with mock.patch.object(hub, "_open_json", return_value=metadata), mock.patch.object(hub, "_fetch_bytes", return_value=archive_data):
            review = hub.handle(self.request("review", source="npm:demo"))["review"]
        self.assertEqual(review["source"], "npm:demo@1.2.3")
        self.assertEqual(review["readme"], "Useful readme")
        self.assertIn("package.json", review["fileContents"])
        self.assertIn("extensions/index.ts", review["fileContents"])
        self.assertTrue(any("scripts" in warning for warning in review["warnings"]))
        self.assertTrue(any("dependencies" in warning for warning in review["warnings"]))

    def test_search_uses_thirty_item_pages(self):
        response = {"total": 1, "objects": [{"package": {"name": "demo", "version": "2.0.0", "description": "Demo"}}]}
        with mock.patch.object(hub, "_open_json", return_value=response) as opened:
            result = hub.handle(self.request("search", query="tools", offset=30))
        self.assertEqual(result["results"][0]["source"], "npm:demo@2.0.0")
        self.assertIn("size=30", opened.call_args.args[0])
        self.assertEqual(result["offset"], 30)

    def test_manifest_exact_files_and_exclusions(self):
        package = self.base / "manifest-package"
        (package / "extensions").mkdir(parents=True)
        included = package / "extensions" / "index.ts"
        excluded = package / "extensions" / "legacy.ts"
        included.write_text("included", encoding="utf-8")
        excluded.write_text("excluded", encoding="utf-8")
        manifest = {"pi": {"extensions": ["extensions/index.ts", "extensions/*.ts", "!extensions/legacy.ts"]}}
        self.assertEqual(hub._manifest_paths(package, manifest, "extensions"), [included])

    def test_github_url_resolves_to_commit_and_previews_extensionless_readme(self):
        commit = "a" * 40
        commands = []
        def run(argv, cwd, timeout):
            commands.append(argv)
            if "ls-remote" in argv:
                return subprocess.CompletedProcess(argv, 0, commit + "\tHEAD\n", "")
            if "checkout" in argv:
                (cwd / "README").write_text("A useful README without a suffix.\n")
            return subprocess.CompletedProcess(argv, 0, commit + "\n" if "rev-parse" in argv else "", "")
        with mock.patch.object(hub, "_run", side_effect=run):
            result = hub.handle(self.request("review", source="https://github.com/example/demo"))["review"]
        self.assertEqual(result["source"], "git:https://github.com/example/demo.git@" + commit)
        self.assertIn("useful README", result["readme"])
        self.assertTrue(any("fetch" in argv and argv[-1] == commit for argv in commands))
        self.assertEqual(hub._parse_github("git:github.com/example/demo@Release/V1")[1], "Release/V1")
        with self.assertRaises(hub.HubError):
            hub._parse_github("https://github.com/example/demo@--upload-pack=bad")

    def test_extension_listing_uses_entrypoints_and_external_file_packages(self):
        root = self.agent / "extensions" / "demo"
        root.mkdir(parents=True)
        (root / "index.ts").write_text("export default function(pi) {}")
        (root / "helper.ts").write_text("export const helper = 1;")
        external = self.project / "external.ts"
        external.write_text("export default function(pi) {}")
        self.write_settings({"packages": [str(external)]})
        resources = hub.handle(self.request("list"))["resources"]
        self.assertEqual({r["name"] for r in resources}, {"demo", "external"})
        self.assertFalse(next(r for r in resources if r["name"] == "external")["editable"])

    def test_npm_root_lookup_does_not_walk_dependencies_before_direct_package(self):
        package = self.agent / "npm/node_modules/@example/demo"
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"name": "@example/demo"}))
        with mock.patch.object(hub.os, "walk", side_effect=AssertionError("unnecessary dependency scan")):
            self.assertEqual(hub._package_roots("npm:@example/demo@1.0.0", hub._scope(self.request("list"))), [package])

    def test_scaffolds_use_pi_api_and_guard_blocks_without_ui(self):
        tool = hub.handle(self.request("scaffold", kind="extensions", template="tool", name="demo-tool", description="Demo"))["body"]
        guard = hub.handle(self.request("scaffold", kind="extensions", template="guard", name="demo-guard", description="Demo"))["body"]
        self.assertIn('from "typebox"', tool)
        self.assertIn('event.toolName !== "bash"', guard)
        self.assertIn("!ctx.hasUI", guard)
        self.assertIn("ctx.ui.confirm", guard)


if __name__ == "__main__":
    unittest.main()
