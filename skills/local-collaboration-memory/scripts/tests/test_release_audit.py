"""Release gates are tested on isolated, synthetic source directories."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zipfile

import audit_release as audit


class ReleaseAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="local-release-gate-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "source"
        self.root.mkdir()
        (self.root / "SKILL.md").write_text("Synthetic source\n", encoding="utf-8", newline="\n")
        root_patch = patch.object(audit, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    def manifest(self):
        path = self.root / audit.MANIFEST
        path.write_text(json.dumps(audit.manifest_for(audit.inventory())), encoding="utf-8")
        return path

    def test_modified_or_extra_source_is_rejected(self):
        manifest = json.loads(self.manifest().read_text(encoding="utf-8"))
        (self.root / "SKILL.md").write_text("Changed source", encoding="utf-8")
        with self.assertRaises(ValueError):
            audit.verify_manifest(audit.inventory(), manifest)
        (self.root / "extra.md").write_text("Unexpected", encoding="utf-8")
        with self.assertRaises(ValueError):
            audit.verify_manifest(audit.inventory(), manifest)

    def test_private_binary_and_hidden_files_fail_closed(self):
        for name, content in (("memory.sqlite3", b"private data"), (".notes.md", b"hidden"), ("binary.md", b"\x00")):
            path = self.root / name
            path.write_bytes(content)
            with self.subTest(name=name), self.assertRaises(ValueError):
                audit.inventory()
            path.unlink()

    def test_credential_profile_and_native_id_are_detected(self):
        examples = {
            "credential-token": "sk-" + "x" * 40,
            "profile-path": "C:" + "/Users/" + "synthetic-user/project",
            "native-identity-literal": str(uuid.uuid4()),
        }
        for category, text in examples.items():
            findings = audit.scan({"example.md": text.encode()}, [])
            self.assertIn(category, {entry["category"] for entry in findings})

    def test_external_private_markers_are_not_echoed(self):
        marker = "sensitive-fixture-phrase"
        findings = audit.scan({"example.md": marker.encode()}, [marker])
        self.assertEqual(findings, [{"path": "example.md", "category": "private-marker"}])
        self.assertNotIn(marker, json.dumps(findings))
        forbidden = self.root / "denied.json"
        forbidden.write_text(json.dumps([marker]), encoding="utf-8")
        with self.assertRaises(ValueError):
            audit.private_strings(forbidden)

    def test_archive_is_exact_and_has_neutral_metadata(self):
        manifest = self.manifest()
        target = self.parent / "release.zip"
        with contextlib.redirect_stdout(io.StringIO()):
            result = audit.main(["--manifest", str(manifest), "--archive", str(target)])
        self.assertEqual(result, 0)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(set(archive.namelist()), {
                "local-collaboration-memory/SKILL.md",
                "local-collaboration-memory/release-manifest.json"})
            for member in archive.infolist():
                self.assertEqual(member.date_time, (2026, 1, 1, 0, 0, 0))
                self.assertEqual(member.extra, b"")
            self.assertEqual(archive.read("local-collaboration-memory/SKILL.md"), b"Synthetic source\n")


if __name__ == "__main__":
    unittest.main()
