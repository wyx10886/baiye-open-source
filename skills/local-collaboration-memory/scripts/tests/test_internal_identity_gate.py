import uuid
"""Only real user threads may enter the main conversation registry."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from local_memory.integration import handle_hook
from local_memory.session_registry import SessionRegistry
from local_memory.store import Store
from local_memory.title_sync import native_identity, synchronize


class InternalIdentityGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="local-internal-gate-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "private"
        self.host = self.root / "codex"
        self.host.mkdir()
        self.store = Store(self.home)
        self.registry = SessionRegistry(self.store)
        self.native = str(uuid.UUID(int=123, version=4))
        (self.home / "settings.json").write_text(json.dumps({"host_title_sync":{
            "enabled":True,"binary":sys.executable,"codex_home":str(self.host)}}),encoding="utf-8")

    def metadata(self, thread_source, source="vscode", parent=None):
        with closing(sqlite3.connect(self.host / "state_5.sqlite")) as con, con:
            con.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY,source TEXT,thread_source TEXT,attached_thread_parent_id TEXT)")
            con.execute("INSERT OR REPLACE INTO threads VALUES(?,?,?,?)",(self.native,source,thread_source,parent))

    def rollout(self, thread_source=None):
        folder = self.host / "sessions" / "2026" / "11" / "18"
        folder.mkdir(parents=True,exist_ok=True)
        payload={"id":self.native,"source":"vscode"}
        if thread_source is not None: payload["thread_source"]=thread_source
        (folder / ("rollout-test-"+self.native+".jsonl")).write_text(
            json.dumps({"type":"session_meta","payload":payload})+"\n",encoding="utf-8")

    def test_explicit_internal_sources_override_interactive_source(self):
        for label in ["guardian_review","memory_consolidation","thread_title","future_feature"]:
            with self.subTest(label=label):
                self.metadata(label)
                self.assertEqual(native_identity(self.home,self.native)["status"],"internal")

    def test_internal_index_cannot_be_laundered_by_an_old_user_rollout(self):
        self.metadata("guardian_review")
        self.rollout()
        result=native_identity(self.home,self.native)
        self.assertEqual(result["status"],"internal")
        self.assertEqual(result["evidence"],"runtime_index")

    def test_internal_rollout_also_stays_internal(self):
        self.rollout("memory_consolidation")
        self.assertEqual(native_identity(self.home,self.native)["status"],"internal")

    def test_explicit_user_owned_tool_created_thread_is_main(self):
        self.metadata("agent_created_thread")
        self.assertEqual(native_identity(self.home,self.native)["status"],"main")

    def test_user_owned_tool_created_rollout_is_main_but_parent_still_wins(self):
        self.rollout("agent_created_thread")
        self.assertEqual(native_identity(self.home,self.native)["status"],"main")
        self.metadata("agent_created_thread",parent="fixture-parent")
        self.assertEqual(native_identity(self.home,self.native)["status"],"subagent")

    def test_missing_thread_source_retains_old_host_compatibility(self):
        self.metadata(None)
        self.assertEqual(native_identity(self.home,self.native)["status"],"main")

    def test_user_marker_is_still_main_but_parent_and_subagent_win(self):
        self.metadata("user")
        self.assertEqual(native_identity(self.home,self.native)["status"],"main")
        self.metadata("user",parent="fixture-parent")
        self.assertEqual(native_identity(self.home,self.native)["status"],"subagent")
        self.metadata("user",source=json.dumps({"subagent":{"thread_spawn":{}}}))
        self.assertEqual(native_identity(self.home,self.native)["status"],"subagent")

    def test_internal_hook_never_allocates_or_synchronizes(self):
        self.metadata("guardian_review")
        with patch("local_memory.title_sync.synchronize") as sync:
            result=handle_hook(self.home,{"hook_event_name":"UserPromptSubmit","session_id":self.native,"prompt":"fixture"})
        self.assertEqual(self.registry.list(),[])
        sync.assert_not_called()
        self.assertNotIn("本会话固定编号",result["hookSpecificOutput"]["additionalContext"])

    def test_internal_direct_sync_never_reads_or_changes_host_title(self):
        self.metadata("thread_title")
        row=self.registry.ensure(thread_id=self.native,source="historical polluted fixture")
        with patch("local_memory.title_sync.TitleClient") as client:
            result=synchronize(self.home,row,source="fixture")
        self.assertEqual(result["status"],"skipped_internal")
        client.assert_not_called()

    def test_unknown_internal_hook_does_not_poison_next_main_number(self):
        self.metadata("future_feature")
        with patch("local_memory.title_sync.synchronize",return_value={"status":"ok"}):
            handle_hook(self.home,{"hook_event_name":"UserPromptSubmit","session_id":self.native,"prompt":"fixture"})
            self.metadata("user")
            handle_hook(self.home,{"hook_event_name":"UserPromptSubmit","session_id":self.native,"prompt":"fixture"})
        self.assertEqual([row["code"] for row in self.registry.list()],["A00"])


if __name__=="__main__": unittest.main()
