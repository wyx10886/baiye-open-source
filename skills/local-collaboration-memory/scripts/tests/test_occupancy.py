"""Complete local chat/workspace inventory, including archives and hidden entries."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from local_memory.occupancy import scan_occupancy


class OccupancyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="local-occupancy-")
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.home=self.base/"private";self.home.mkdir()
        self.host=self.base/"host";self.host.mkdir()
        self.workspace=self.base/"work";self.workspace.mkdir()
        (self.home/"settings.json").write_text(json.dumps({"host_title_sync":{"codex_home":str(self.host)},
            "session_recycling":{"workspace_roots":[str(self.workspace)]}}),encoding="utf-8")
        with closing(sqlite3.connect(self.host/"state_5.sqlite")) as con,con:
            con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY,name TEXT,title TEXT,source TEXT,thread_source TEXT,attached_thread_parent_id TEXT,archived INTEGER)")

    def thread(self,index,name,*,source="vscode",kind="user",archived=0):
        native=f"00000000-0000-4000-8000-{index:012d}"
        with closing(sqlite3.connect(self.host/"state_5.sqlite")) as con,con:
            con.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?)",(native,name,"unrelated preview",source,kind,None,archived))
        return native

    def test_names_and_archived_chats_both_reserve_codes(self):
        one=self.thread(1,"[A01] active")
        six=self.thread(6,"[A06] archived",archived=1)
        result=scan_occupancy(self.home)
        self.assertTrue(result["complete"],result)
        self.assertIn(six,result["live_thread_ids"])
        self.assertEqual(result["thread_codes"]["A06"],[six])
        self.assertEqual(result["thread_codes"]["A01"],[one])

    def test_child_and_internal_titles_are_not_main_chat_occupancy(self):
        self.thread(1,"[A06] child",kind="subagent",source=json.dumps({"subagent":{"thread_spawn":{}}}))
        self.thread(2,"[A07] review",kind="guardian_review")
        tool=self.thread(3,"[A05] actual tool-created user chat",kind="agent_created_thread")
        result=scan_occupancy(self.home)
        self.assertTrue(result["complete"],result)
        self.assertEqual(result["live_thread_ids"],[tool])
        self.assertNotIn("A06",result["thread_codes"])

    def test_ascii_word_and_cache_prefixes_are_not_conversation_codes(self):
        for name in ("a11y-debugging","a12d1a57deadbeef","A06notes","A010_cache"):
            (self.workspace/name).mkdir()
        self.thread(1,"a11y ordinary title")
        for name in ("A06-1_task","a07_data","A09.md","AA06_归属"):
            (self.workspace/name).write_text("anonymous",encoding="utf-8")
        result=scan_occupancy(self.home)
        self.assertTrue(result["complete"],result)
        for code in ("A11","A12","A01"):
            self.assertNotIn(code,result["directory_codes"])
        self.assertNotIn("A11",result["thread_codes"])
        for code in ("A06","A07","A09","AA06"):
            self.assertIn(code,result["directory_codes"])

    def test_hidden_nested_entries_and_code_boundaries(self):
        folder=self.workspace/".hidden"/"nested"/"A06-1_task";folder.mkdir(parents=True)
        (self.workspace/"A010_unrelated").mkdir()
        (self.workspace/"AA06_more").mkdir()
        result=scan_occupancy(self.home)
        self.assertTrue(result["complete"],result)
        self.assertIn(str(folder.resolve()),result["directory_codes"]["A06"])
        self.assertNotIn("A01",result["directory_codes"])
        self.assertIn("AA06",result["directory_codes"])

    def test_missing_host_index_is_incomplete_not_an_empty_inventory(self):
        (self.host/"state_5.sqlite").unlink()
        result=scan_occupancy(self.home)
        self.assertFalse(result["complete"])
        self.assertTrue(result["errors"])

    def test_newest_unsupported_state_never_falls_back_to_stale_index(self):
        self.thread(1,"[A06] existing")
        with closing(sqlite3.connect(self.host/"state_99.sqlite")) as con,con:con.execute("CREATE TABLE unrelated(x)")
        result=scan_occupancy(self.home)
        self.assertFalse(result["complete"])

    def test_unknown_thread_class_prevents_recycling(self):
        self.thread(1,"[A06] maybe user",kind="future_unknown_type")
        self.assertFalse(scan_occupancy(self.home)["complete"])

    def test_invalid_explicit_host_configuration_never_falls_back(self):
        for config in ([], {"codex_home":"relative-host"}, {"codex_home":""}, {"codex_home":42}):
            with self.subTest(config=config):
                (self.home/"settings.json").write_text(json.dumps({"host_title_sync":config,
                    "session_recycling":{"workspace_roots":[str(self.workspace)]}}),encoding="utf-8")
                result=scan_occupancy(self.home)
                self.assertFalse(result["complete"])
                self.assertTrue(any(item["kind"]=="invalid_host_configuration" for item in result["errors"]),result)

    def test_directory_rechecked_for_reparse_before_traversal(self):
        nested=self.workspace/"nested";nested.mkdir();(nested/"A06_private").mkdir()
        from local_memory import occupancy
        original=occupancy._linked
        with patch.object(occupancy,"_linked",side_effect=lambda path: path==nested or original(path)):
            result=scan_occupancy(self.home)
        self.assertFalse(result["complete"])
        self.assertNotIn("A06",result["directory_codes"])

    def test_missing_workspace_is_incomplete(self):
        result=scan_occupancy(self.home,workspace_roots=[str(self.base/"missing")])
        self.assertFalse(result["complete"])

    def link(self,link,target):
        try:link.symlink_to(target,target_is_directory=True)
        except OSError:
            if os.name!="nt":raise
            result=subprocess.run(["cmd","/c","mklink","/J",str(link),str(target)],capture_output=True)
            if result.returncode:self.skipTest("Directory links unavailable")

    def test_external_directory_link_is_not_traversed_or_ignored(self):
        external=self.base/"outside";external.mkdir();(external/"A06_sensitive").mkdir()
        link=self.workspace/"external-link"
        self.link(link,external)
        result=scan_occupancy(self.home)
        self.assertFalse(result["complete"])
        self.assertNotIn("A06",result["directory_codes"])

    def test_internal_link_alias_is_complete_only_when_target_is_enumerated(self):
        target=self.workspace/"real";target.mkdir();(target/"A06_task").mkdir()
        self.link(self.workspace/"alias",target)
        result=scan_occupancy(self.home)
        self.assertTrue(result["complete"],result)
        self.assertEqual(len(result["directory_codes"]["A06"]),1)

    def test_snapshot_does_not_persist_or_return_full_chat_titles(self):
        self.thread(1,"[A06] private fixture title")
        result=scan_occupancy(self.home)
        self.assertNotIn("private fixture title",json.dumps(result))
        self.assertFalse((self.home/"session-registry.sqlite3").exists())


if __name__=="__main__":unittest.main()
