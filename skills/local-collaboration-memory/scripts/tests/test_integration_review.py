"""Regression tests for installation preservation and completion fallbacks."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from local_memory.integration import handle_hook, install, notify
from local_memory.store import Store, ValidationError


class IntegrationPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="local-integration-review-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "private"
        self.codex = self.root / "codex"
        self.codex.mkdir()

    def test_indented_valid_toml_notify_is_preserved(self):
        original = 'model = "example"\n  notify = ["original-notifier", "arg"]\n'
        path = self.codex / "config.toml"
        path.write_text(original, encoding="utf-8")
        install(self.home, self.codex)
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["model"], "example")
        self.assertEqual(json.loads((self.home / "previous-notify.json").read_text(encoding="utf-8")),
                         ["original-notifier", "arg"])

    def test_changing_private_home_preserves_original_notification_chain(self):
        (self.codex / "config.toml").write_text('notify = ["original-notifier", "arg"]\n', encoding="utf-8")
        install(self.home, self.codex)
        new_home = self.root / "private-moved"
        install(new_home, self.codex)
        preserved = new_home / "previous-notify.json"
        self.assertTrue(preserved.is_file(), "Changing data home must migrate the original notification chain")
        self.assertEqual(json.loads(preserved.read_text(encoding="utf-8")), ["original-notifier", "arg"])

    def test_fresh_install_leaves_optional_host_title_sync_disabled(self):
        install(self.home, self.codex)
        settings = json.loads((self.home / "settings.json").read_text(encoding="utf-8"))
        self.assertIs(settings["host_title_sync"]["enabled"], False)
        native_id = "00000000-0000-4000-8000-000000000001"
        with patch("local_memory.title_sync.TitleClient") as client:
            handle_hook(self.home, {"hook_event_name": "SessionStart", "session_id": native_id})
            notify(self.home, {"type": "agent-turn-complete", "thread-id": native_id,
                               "last-assistant-message": "Completed a synthetic fixture milestone."})
        client.assert_not_called()

    def test_install_preserves_explicit_title_sync_and_other_settings(self):
        self.home.mkdir()
        settings = {"host_title_sync": {"enabled": True, "binary": "chosen-codex",
                    "codex_home": "chosen-home", "timeout_seconds": 9},
                    "projects": [{"slug": "synthetic-project", "aliases": ["Example project"]}],
                    "session_recycling": {"enabled": True, "workspace_roots": ["chosen-workspace"]}}
        path = self.home / "settings.json"
        path.write_text(json.dumps(settings), encoding="utf-8")
        install(self.home, self.codex)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), settings)

    def test_fresh_install_keeps_memory_empty(self):
        install(self.home, self.codex)
        self.assertEqual(Store(self.home).list_memories(include_inactive=True), [])

    def test_capture_and_log_failure_do_not_prevent_original_notification(self):
        self.home.mkdir()
        (self.home / "previous-notify.json").write_text('["original-notifier"]', encoding="utf-8")
        payload = {"type": "agent-turn-complete", "last-assistant-message": "completed fixture milestone"}
        with patch("local_memory.integration.Store", side_effect=OSError("unwritable storage")), \
             patch("local_memory.integration._log_failure", side_effect=OSError("unwritable log")), \
             patch("local_memory.integration.subprocess.run") as run:
            notify(self.home, payload)
        self.assertTrue(run.called)
        self.assertEqual(run.call_args.args[0][0], "original-notifier")
        self.assertEqual(json.loads(run.call_args.args[0][-1]), payload)

    def test_missing_source_launcher_is_rejected_before_config_changes(self):
        original = 'model = "example"\n'
        path = self.codex / "config.toml"
        path.write_text(original, encoding="utf-8")
        missing_module = self.root / "absent-source" / "local_memory" / "integration.py"
        with patch("local_memory.integration.__file__", str(missing_module)):
            with self.assertRaises(ValidationError):
                install(self.home, self.codex)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_package_launcher_works_without_checkout_root_entry(self):
        package_source = Path(__file__).resolve().parents[1] / "local_memory"
        installed_package = self.root / "site-packages" / "local_memory"
        shutil.copytree(package_source, installed_package, ignore=shutil.ignore_patterns("__pycache__"))
        launcher = installed_package / "launcher.py"
        self.assertFalse((installed_package.parent / "entry.py").exists())
        result = subprocess.run([sys.executable, str(launcher), "--home", str(self.home), "install",
                                 "--codex-home", str(self.codex)], cwd=self.root,
                                capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertTrue(json.loads(result.stdout)["installed"])
        parsed = tomllib.loads((self.codex / "config.toml").read_text(encoding="utf-8"))
        mcp = parsed["mcp_servers"]["local_memory"]
        self.assertEqual(Path(mcp["args"][0]), launcher)
        ping = subprocess.run([mcp["command"], *mcp["args"]], cwd=self.root,
                              input='{"jsonrpc":"2.0","id":1,"method":"ping"}\n',
                              capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertEqual(json.loads(ping.stdout)["result"], {})


if __name__ == "__main__":
    unittest.main()
