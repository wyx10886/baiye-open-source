import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

from local_memory.integration import install,handle_hook,notify,resolve_project,doctor
from local_memory.store import Store


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="local-integration-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root/"private"
        self.codex = self.root/"codex"
        self.codex.mkdir()

    def test_preserve_and_repeat_install(self):
        original = 'model="example"\nnotify = ["old-program", "arg"]\n[mcp_servers.existing]\ncommand="existing-command"\n[model_providers.custom]\nbase_url="https://example.invalid"\n'
        (self.codex/"config.toml").write_text(original,encoding="utf-8")
        (self.codex/"AGENTS.md").write_text("existing human rule",encoding="utf-8")
        (self.codex/"hooks.json").write_text(json.dumps({"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"existing-hook"}]}]}}),encoding="utf-8")
        result = install(self.home,self.codex)
        after = tomllib.loads((self.codex/"config.toml").read_text(encoding="utf-8"))
        self.assertEqual(after["model"],"example")
        self.assertEqual(after["mcp_servers"]["existing"],{"command":"existing-command"})
        self.assertTrue(result["notify_preserved"])
        self.assertEqual(json.loads((self.home/"previous-notify.json").read_text(encoding="utf-8")),["old-program","arg"])
        install(self.home,self.codex)
        self.assertEqual(after,tomllib.loads((self.codex/"config.toml").read_text(encoding="utf-8")))
        hooks = json.loads((self.codex/"hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(len(hooks["hooks"]["SessionStart"]),2)
        self.assertIn("existing-hook",json.dumps(hooks))
        agents = (self.codex/"AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(agents.startswith("existing human rule"))
        self.assertEqual(agents.count("<!-- local-memory:start -->"),1)
        self.assertTrue(doctor(self.home,self.codex)["mcp_launcher_exists"])

    def test_hook_actual_cli_context(self):
        store = Store(self.home)
        store.remember(kind="project",title="项目下一步",content="先检查训练集时间边界",source="fixture evidence",scope="project:forecast")
        (self.home/"settings.json").write_text(json.dumps({"projects":[{"slug":"forecast","aliases":["预测项目"],"paths":[str(self.root/"forecast")]}]}),encoding="utf-8")
        self.assertEqual(resolve_project(self.home,self.root/"forecast"/"nested"),"forecast")
        payload = {"hook_event_name":"UserPromptSubmit","prompt":"预测项目接着做","cwd":str(self.root)}
        process = subprocess.run([sys.executable,"-m","local_memory","--home",str(self.home),"hook"],
            input=json.dumps(payload),text=True,encoding="utf-8",capture_output=True,check=True)
        result = json.loads(process.stdout)
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("训练集时间边界",context)
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"],"UserPromptSubmit")

    def test_notify_quarantines_unknown_scope_and_is_silent(self):
        payload = {"type":"agent-turn-complete","thread-id":"t1","turn-id":"turn1","cwd":str(self.root/"unknown"),
                   "last-assistant-message":"已完成测试验证，下一步再核对实际训练结果。","input-messages":["must not persist"]}
        process = subprocess.run([sys.executable,"-m","local_memory","--home",str(self.home),"notify",json.dumps(payload)],
            text=True,encoding="utf-8",capture_output=True,check=True)
        self.assertEqual(process.stdout,"")
        store = Store(self.home)
        rows = store.list_memories()
        self.assertEqual(len(rows),1)
        self.assertTrue(rows[0]["scope"].startswith("project:inbox-"))
        self.assertEqual(store.context("测试")["candidates"],[])
        self.assertNotIn("must not persist",rows[0]["content"])

    def test_hook_failure_is_nonblocking(self):
        process = subprocess.run([sys.executable,"-m","local_memory","--home",str(self.home),"hook"],
            input="malformed-json",text=True,encoding="utf-8",capture_output=True)
        self.assertEqual(process.returncode,0)
        self.assertIn("systemMessage",json.loads(process.stdout))

    def test_known_project_notify(self):
        Store(self.home)
        (self.home/"settings.json").write_text(json.dumps({"projects":[{"slug":"alpha","aliases":["Project Alpha"]}]}),encoding="utf-8")
        payload = {"type":"agent-turn-complete","thread-id":"t2","turn-id":"turn2",
                   "last-assistant-message":"Project Alpha implemented and verified. Next step is a user review."}
        notify(self.home,payload)
        notify(self.home,payload)
        self.assertEqual(len(Store(self.home).list_memories(scope="project:alpha")),1)


if __name__ == "__main__":
    unittest.main()
