import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codex_bark
import install


class CodexBarkTests(unittest.TestCase):
    def test_loads_lifecycle_stop_event_from_stdin(self):
        event = codex_bark.load_event([], '{"hook_event_name":"Stop"}')
        self.assertEqual(event["hook_event_name"], "Stop")

    def test_loads_legacy_notify_event_from_argument(self):
        event = codex_bark.load_event(['{"type":"agent-turn-complete"}'], "")
        self.assertEqual(event["type"], "agent-turn-complete")

    def test_should_notify_only_completion_events(self):
        self.assertTrue(codex_bark.should_notify({"hook_event_name": "Stop"}))
        self.assertTrue(codex_bark.should_notify({"type": "agent-turn-complete"}))
        self.assertFalse(codex_bark.should_notify({"hook_event_name": "PreToolUse"}))
        self.assertFalse(codex_bark.should_notify({"type": "approval-requested"}))

    def test_build_payload_includes_workspace_and_message(self):
        payload = codex_bark.build_bark_payload(
            {
                "hook_event_name": "Stop",
                "cwd": "/tmp/example",
                "model": "gpt-test",
                "turn_id": "turn-1",
                "last_assistant_message": "done",
            },
            title_prefix="Done",
            group="Codex",
            sound=None,
            url=None,
        )
        self.assertEqual(payload["title"], "Done")
        self.assertEqual(payload["group"], "Codex")
        self.assertIn("Workspace: example", payload["body"])
        self.assertIn("Model: gpt-test", payload["body"])
        self.assertIn("done", payload["body"])

    def test_send_bark_posts_json(self):
        response = mock.Mock()
        response.read.return_value = json.dumps({"code": 200, "message": "success"}).encode()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=None)

        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            result = codex_bark.send_bark("abc/123", {"title": "T", "body": "B"}, timeout=1)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.day.app/abc%2F123")
        self.assertEqual(json.loads(request.data.decode()), {"title": "T", "body": "B"})
        self.assertEqual(result["code"], 200)

    def test_installer_quotes_env_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / "codex-bark.env"
            changed = install.write_env(
                env_path,
                "device key",
                "https://api.day.app",
                Path(tmpdir) / "codex-bark.log",
            )
            content = env_path.read_text()

        self.assertTrue(changed)
        self.assertIn("BARK_DEVICE_KEY='device key'", content)
        self.assertIn("BARK_TITLE='Codex task complete'", content)

    def test_register_device_token_returns_device_key(self):
        response = mock.Mock()
        response.read.return_value = json.dumps(
            {"code": 200, "data": {"device_key": "registered-key"}}
        ).encode()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=None)

        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            key = install.register_device_token("https://api.day.app", "apns-token")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.day.app/register")
        self.assertEqual(request.data.decode(), "devicetoken=apns-token")
        self.assertEqual(key, "registered-key")


if __name__ == "__main__":
    unittest.main()
