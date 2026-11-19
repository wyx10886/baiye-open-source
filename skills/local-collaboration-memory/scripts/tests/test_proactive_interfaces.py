"""Verify scheduling uses the registered daily entry and real user activity."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_memory.cli import build_parser, dispatch, main
from local_memory.daily_chat import DailyChat
from local_memory.integration import _is_automated_prompt, handle_hook
from local_memory.proactive import ProactiveChat
from local_memory.session_registry import SessionRegistry
from local_memory.store import Store, ValidationError


class ProactiveInterfaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="local-proactive-interface-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "private"
        self.store = Store(self.home)
        registry = SessionRegistry(self.store)
        registry.ensure(thread_id="current", source="verified fixture identity")
        registry.ensure(thread_id="other", source="verified fixture identity")
        DailyChat(self.store).bind(thread_id="current", title="Daily", source="explicit fixture request",
                                   expected_revision=0)

    def dispatch_payload(self, action, payload):
        args = build_parser().parse_args(["proactive", action, "--input", "-"])
        with patch("sys.stdin", io.StringIO(json.dumps(payload))):
            return dispatch(args, self.home)

    def test_status_does_not_open_the_main_memory_store(self):
        args = build_parser().parse_args(["proactive", "status"])
        with patch("local_memory.cli.Store") as store:
            result = dispatch(args, self.home)
        store.assert_not_called()
        self.assertFalse(result["enabled"])

    def test_cli_uses_the_actual_daily_pointer(self):
        with patch("local_memory.proactive.ProactiveChat.evaluate", return_value={"eligible": False}) as run:
            self.dispatch_payload("evaluate", {"current_thread_id": "current", "busy": False})
        self.assertEqual(run.call_args.kwargs["daily_thread_id"], "current")

    def test_cli_rejects_a_forged_or_stale_daily_pointer(self):
        with self.assertRaises(ValidationError):
            self.dispatch_payload("claim", {"current_thread_id": "other", "daily_thread_id": "other"})

    def test_cli_invalid_json_object_has_structured_error(self):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.StringIO("[]")), redirect_stdout(out), redirect_stderr(err):
            code = main(["--home", str(self.home), "proactive", "configure", "--input", "-"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err.getvalue())["error"], "ValidationError")

    def hook(self, prompt, native="current"):
        with patch("local_memory.title_sync.native_identity", return_value={"status": "main"}), \
             patch("local_memory.title_sync.synchronize", return_value={"status": "ok"}):
            return handle_hook(self.home, {"hook_event_name": "UserPromptSubmit", "session_id": native,
                                          "prompt": prompt, "cwd": str(self.home)})

    def test_only_real_daily_user_messages_update_interaction_metadata(self):
        ProactiveChat(self.home).configure(enabled=True, source="synthetic explicit authorization")
        with patch("local_memory.proactive.ProactiveChat.observe_activity") as observe:
            self.hook("continue our conversation")
            self.assertEqual(observe.call_count, 1)
            self.assertEqual(observe.call_args.kwargs, {"thread_id": "current"})
            self.hook("a different task", native="other")
            self.assertEqual(observe.call_count, 1)

    def test_automations_never_count_as_human_activity_or_processing_record_use(self):
        for prompt in ("[local-proactive-chat] prepare a check-in", "[local-housekeeping] scan only",
                       "<heartbeat><automation_id>fixture</automation_id></heartbeat>"):
            with self.subTest(prompt=prompt), \
                 patch("local_memory.proactive.ProactiveChat.observe_activity") as observe, \
                 patch("local_memory.retention.Retention.activity") as activity:
                self.hook(prompt)
                observe.assert_not_called()
                activity.assert_not_called()

    def test_plain_text_mentions_are_not_automatically_scheduled_work(self):
        self.assertFalse(_is_automated_prompt("What does <heartbeat> mean?"))
        self.assertFalse(_is_automated_prompt("Please adjust [local-proactive-chat] to start later"))
        self.assertFalse(_is_automated_prompt("Explain <heartbeat><automation_id>fixture</automation_id>"))
        self.assertFalse(_is_automated_prompt(None))

    def test_human_request_quoting_a_schedule_marker_is_still_real_activity(self):
        ProactiveChat(self.home).configure(enabled=True, source="synthetic explicit authorization")
        with patch("local_memory.proactive.ProactiveChat.observe_activity") as observe, \
             patch("local_memory.retention.Retention.activity") as activity:
            self.hook("Please adjust [local-proactive-chat] to start later")
            observe.assert_called_once()
            activity.assert_called_once()


if __name__ == "__main__":
    unittest.main()
