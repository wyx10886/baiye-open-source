"""Exercise the shipped stdio process, not an in-process protocol imitation."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="local-mcp-test-")

    def tearDown(self):
        self.tmp.cleanup()

    def run_requests(self, messages):
        lines = [message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
                 for message in messages]
        result = subprocess.run([sys.executable, "-m", "local_memory", "--home", self.tmp.name, "mcp"],
                                cwd=ROOT, input="\n".join(lines) + "\n", text=True,
                                encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Every stdout line must be protocol JSON; diagnostic noise would break an actual client.
        responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return responses

    def initialize(self):
        return [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "local-acceptance-test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"}]

    def unpack_tool(self, response):
        self.assertNotIn("error", response, response)
        result = response["result"]
        self.assertFalse(result.get("isError", False), result)
        text = "\n".join(item["text"] for item in result["content"] if item.get("type") == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def test_handshake_tool_schemas_and_ping(self):
        responses = self.run_requests(self.initialize() + [
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"}])
        by_id = {response.get("id"): response for response in responses}
        self.assertEqual(by_id[1]["result"]["protocolVersion"], "2024-11-05")
        tools = by_id[2]["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertTrue({"local_context", "local_remember", "local_revise", "local_search"} <= names)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
        self.assertIn("result", by_id[3])
        self.assertEqual(len(responses), 3, "Notifications must not produce JSON-RPC replies")

    def test_remember_and_retrieve_through_real_process(self):
        messages = self.initialize() + [
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "local_remember", "arguments": {
                    "kind": "project", "scope": "project:fixture", "title": "核查成果",
                    "content": "传感器时间间隔已核查，下一步确认测量单位。",
                    "source": "synthetic protocol acceptance fixture", "status": "confirmed"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "local_search", "arguments": {"query": "测量单位", "scope": "project:fixture"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "local_context", "arguments": {"query": "核查", "project": "fixture"}}}]
        responses = {response.get("id"): response for response in self.run_requests(messages)}
        remembered = self.unpack_tool(responses[2])
        retrieved = self.unpack_tool(responses[3])
        context = self.unpack_tool(responses[4])
        self.assertIn("id", remembered)
        self.assertIn(remembered["id"], json.dumps(retrieved, ensure_ascii=False))
        self.assertIn("测量单位", json.dumps(context, ensure_ascii=False))
        # A new server process reads the same database after the first one exits.
        again = self.run_requests(self.initialize() + [messages[3]])
        self.assertIn("测量单位", json.dumps(again, ensure_ascii=False))

    def test_protocol_and_tool_errors_do_not_crash_server(self):
        messages = self.initialize() + [
            "{broken json",
            {"jsonrpc": "2.0", "id": 2, "method": "nonexistent/method"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "nonexistent_tool", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "local_remember", "arguments": {"kind": "event"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "ping"}]
        responses = self.run_requests(messages)
        by_id = {response.get("id"): response for response in responses}
        self.assertTrue(any(response.get("error", {}).get("code") == -32700 for response in responses))
        self.assertEqual(by_id[2]["error"]["code"], -32601)
        for index in (3, 4):
            self.assertTrue("error" in by_id[index] or by_id[index].get("result", {}).get("isError"))
        self.assertIn("result", by_id[5])


if __name__ == "__main__":
    unittest.main()
