import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codex_bark
import install


def sh_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


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

    def test_build_payload_includes_project_question_and_result(self):
        payload = codex_bark.build_bark_payload(
            {
                "hook_event_name": "Stop",
                "cwd": "/tmp/example",
                "model": "gpt-test",
                "turn_id": "turn-1",
                "prompt": "ship the Bark hook",
                "last_assistant_message": "done",
            },
            title_prefix="Done",
            group="Codex",
            sound=None,
            url=None,
        )
        self.assertEqual(payload["title"], "Done")
        self.assertEqual(payload["group"], "Codex")
        self.assertIn("Project: example", payload["body"])
        self.assertIn("Question: ship the Bark hook", payload["body"])
        self.assertIn("Result: done", payload["body"])
        self.assertNotIn("Duration:", payload["body"])
        self.assertNotIn("(2m 5s)", payload["body"])
        self.assertNotIn("Model:", payload["body"])
        self.assertNotIn("Time:", payload["body"])
        self.assertNotIn("\n\n", payload["body"])

    def test_duration_is_never_shown(self):
        payload = codex_bark.build_bark_payload(
            {
                "hook_event_name": "Stop",
                "cwd": "/tmp/example",
                "duration_seconds": 125,
                "prompt": "small task",
                "last_assistant_message": "done",
            },
            title_prefix="Complete",
            group="Codex",
            sound=None,
            url=None,
        )

        self.assertIn("Project: example\n", payload["body"])
        self.assertNotIn("Duration:", payload["body"])
        self.assertNotIn("(2m", payload["body"])

    def test_question_and_result_have_separate_limits(self):
        payload = codex_bark.build_bark_payload(
            {
                "hook_event_name": "Stop",
                "cwd": "/tmp/example",
                "prompt": "q" * 20,
                "last_assistant_message": "r" * 20,
            },
            title_prefix="Complete",
            group="Codex",
            sound=None,
            url=None,
            task_max_chars=8,
            result_max_chars=10,
        )

        self.assertIn("Question: qqqqqqq...", payload["body"])
        self.assertIn("Result: rrrrrrrrr...", payload["body"])

    def test_reads_task_from_transcript_when_state_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps({"payload": {"type": "user_message", "message": "first task"}}),
                        json.dumps({"payload": {"type": "user_message", "message": "latest task"}}),
                    ]
                ),
                encoding="utf-8",
            )
            task = codex_bark.extract_task({"transcript_path": str(transcript)})

        self.assertEqual(task, "latest task")

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
                Path(tmpdir) / "config.json",
            )
            content = env_path.read_text()

        self.assertTrue(changed)
        self.assertIn("BARK_DEVICE_KEY='device key'", content)
        self.assertIn("BARK_TITLE=Complete", content)
        self.assertNotIn("CODEX_BARK_STATE_DIR=", content)
        self.assertIn("CODEX_BARK_CONFIG=", content)
        self.assertIn("CODEX_BARK_TASK_MAX_CHARS=180", content)
        self.assertIn("CODEX_BARK_RESULT_MAX_CHARS=260", content)

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

    def test_installer_writes_stop_hook_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hooks_path = Path(tmpdir) / "hooks.json"
            env_path = Path(tmpdir) / "env"
            changed = install.install_codex_bark_hooks(hooks_path, env_path)
            data = json.loads(hooks_path.read_text())

        self.assertTrue(changed)
        self.assertIn("Stop", data["hooks"])
        self.assertNotIn("UserPromptSubmit", data["hooks"])
        self.assertIn("codex_bark.py", data["hooks"]["Stop"][0]["hooks"][0]["command"])

    def test_installer_removes_older_user_prompt_hook(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            hooks_path = Path(tmpdir) / "hooks.json"
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": f"python3 {install.HOOK_SCRIPT}",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            install.install_codex_bark_hooks(hooks_path, Path(tmpdir) / "env")
            data = json.loads(hooks_path.read_text())

        self.assertNotIn("UserPromptSubmit", data["hooks"])
        self.assertIn("Stop", data["hooks"])

    def test_installer_reads_existing_device_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / "env"
            env_path.write_text("BARK_DEVICE_KEY='saved key'\n", encoding="utf-8")

            value = install.read_existing_env_value(env_path, "BARK_DEVICE_KEY")

        self.assertEqual(value, "saved key")

    def test_custom_hooks_receive_event_and_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "hook.json"
            command = (
                f"{sh_quote(os.sys.executable)} -c "
                + sh_quote(
                    "import os, pathlib; "
                    f"pathlib.Path({str(output)!r}).write_text(os.environ['CODEX_BARK_HOOK'] + '\\n' + os.environ['CODEX_BARK_PAYLOAD'])"
                )
            )
            codex_bark.run_custom_hooks(
                {"after_notify": [command]},
                "after_notify",
                {"hook_event_name": "Stop"},
                {"title": "T"},
                timeout=5,
                log_file=None,
            )
            content = output.read_text()

        self.assertIn("after_notify", content)
        self.assertIn('"title": "T"', content)


if __name__ == "__main__":
    unittest.main()
