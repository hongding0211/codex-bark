#!/usr/bin/env python3
"""Codex lifecycle hook that sends task-complete notifications via Bark."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


DEFAULT_BARK_SERVER = "https://api.day.app"
DEFAULT_TITLE = "Codex task complete"
DEFAULT_GROUP = "Codex"
MAX_BODY_CHARS = 900
MAX_TASK_CHARS = 72
DEFAULT_HOOK_TIMEOUT = 15.0


class BarkError(RuntimeError):
    """Raised when Bark rejects or cannot receive the notification."""


def _hook_success_output() -> Dict[str, Any]:
    return {"continue": True, "suppressOutput": True}


def load_event(argv: list[str], stdin_text: str) -> Dict[str, Any]:
    """Load hook JSON from stdin or legacy notify JSON from argv[0]."""

    source = stdin_text.strip() or (argv[0].strip() if argv else "")
    if not source:
        return {}

    try:
        value = json.loads(source)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON input: {exc}") from exc

    if not isinstance(value, dict):
        raise ValueError("notification payload must be a JSON object")
    return value


def should_notify(event: Dict[str, Any]) -> bool:
    """Return true for Codex task-complete events."""

    hook_event_name = event.get("hook_event_name")
    if hook_event_name:
        return hook_event_name == "Stop"

    legacy_type = event.get("type")
    return legacy_type == "agent-turn-complete"


def _clean_text(value: Any, limit: int = MAX_BODY_CHARS) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    value = " ".join(value.split())
    if len(value) > limit:
        return value[: limit - 1].rstrip() + "..."
    return value


def state_key(event: Dict[str, Any]) -> str:
    session_id = str(event.get("session_id") or "")
    turn_id = str(event.get("turn_id") or event.get("turn-id") or "")
    transcript_path = str(event.get("transcript_path") or "")
    raw = "\0".join([session_id, turn_id, transcript_path])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def state_path(state_dir: str, event: Dict[str, Any]) -> Path:
    return Path(state_dir).expanduser() / f"{state_key(event)}.json"


def save_turn_state(event: Dict[str, Any], state_dir: str) -> None:
    if not state_dir:
        return
    prompt = _clean_text(event.get("prompt"), limit=MAX_BODY_CHARS)
    path = state_path(state_dir, event)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "started_at": time.time(),
                    "prompt": prompt,
                    "cwd": event.get("cwd"),
                    "session_id": event.get("session_id"),
                    "turn_id": event.get("turn_id") or event.get("turn-id"),
                    "transcript_path": event.get("transcript_path"),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        return


def load_turn_state(event: Dict[str, Any], state_dir: str) -> Dict[str, Any]:
    if not state_dir:
        return {}
    path = state_path(state_dir, event)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_last_user_prompt(transcript_path: Optional[str]) -> str:
    if not transcript_path:
        return ""
    path = Path(transcript_path).expanduser()
    if not path.exists():
        return ""

    last_prompt = ""
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") == "user_message":
                    last_prompt = _clean_text(payload.get("message"), limit=MAX_BODY_CHARS)
    except OSError:
        return ""
    return last_prompt


def extract_task(event: Dict[str, Any], state: Dict[str, Any]) -> str:
    candidates = [
        event.get("prompt"),
        event.get("user_prompt"),
        event.get("task"),
        state.get("prompt"),
        read_last_user_prompt(event.get("transcript_path") or state.get("transcript_path")),
    ]
    for candidate in candidates:
        text = _clean_text(candidate, limit=MAX_BODY_CHARS)
        if text:
            return text
    return "Codex task"


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return ""
    rounded = int(round(seconds))
    minutes, secs = divmod(rounded, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def duration_from_event(event: Dict[str, Any], state: Dict[str, Any]) -> str:
    explicit = event.get("duration_seconds") or event.get("elapsed_seconds")
    if isinstance(explicit, (int, float)):
        return format_duration(float(explicit))

    started_at = state.get("started_at")
    if isinstance(started_at, (int, float)):
        return format_duration(time.time() - float(started_at))
    return ""


def summarize_event(
    event: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
    title_prefix: str = DEFAULT_TITLE,
) -> Tuple[str, str]:
    state = state or {}
    cwd = event.get("cwd") or event.get("workspace") or ""
    cwd_name = Path(cwd).name if cwd else "unknown workspace"

    task = extract_task(event, state)
    task_title = _clean_text(task, limit=MAX_TASK_CHARS)
    title = f"{title_prefix}: {task_title}" if title_prefix else task_title

    last_message = _clean_text(
        event.get("last_assistant_message")
        or event.get("last-assistant-message")
        or event.get("message")
        or "Codex finished the current task.",
        limit=MAX_BODY_CHARS,
    )

    duration = duration_from_event(event, state)
    body_lines = [f"Project: {cwd_name}"]
    if duration:
        body_lines.append(f"Duration: {duration}")
    body_lines.extend(["", f"Result: {last_message}"])

    return title, "\n".join(body_lines)


def build_bark_payload(
    event: Dict[str, Any],
    state: Optional[Dict[str, Any]],
    title_prefix: str,
    group: str,
    sound: Optional[str],
    url: Optional[str],
) -> Dict[str, Any]:
    title, body = summarize_event(event, state=state, title_prefix=title_prefix)
    payload: Dict[str, Any] = {
        "title": title,
        "body": body,
        "group": group,
    }
    if sound:
        payload["sound"] = sound
    if url:
        payload["url"] = url
    return payload


def load_custom_config(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    config_path = Path(path).expanduser()
    if not config_path.exists():
        return {}
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def custom_commands(config: Dict[str, Any], name: str) -> list[str]:
    value = config.get(name, [])
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def run_custom_hooks(
    config: Dict[str, Any],
    name: str,
    event: Dict[str, Any],
    payload: Optional[Dict[str, Any]],
    timeout: float,
    log_file: Optional[str],
) -> None:
    commands = custom_commands(config, name)
    if not commands:
        return

    hook_input = json.dumps(
        {"event": event, "payload": payload, "hook": name},
        ensure_ascii=False,
    )
    env = os.environ.copy()
    env["CODEX_BARK_HOOK"] = name
    env["CODEX_BARK_EVENT"] = json.dumps(event, ensure_ascii=False)
    if payload is not None:
        env["CODEX_BARK_PAYLOAD"] = json.dumps(payload, ensure_ascii=False)

    for command in commands:
        try:
            completed = subprocess.run(
                command,
                input=hook_input,
                text=True,
                shell=True,
                env=env,
                timeout=timeout,
                capture_output=True,
                check=False,
            )
        except subprocess.TimeoutExpired:
            write_log(log_file, f"{datetime.now().isoformat()} HOOK_TIMEOUT {name} {command}")
            continue
        if completed.returncode != 0:
            detail = _clean_text(completed.stderr or completed.stdout, limit=300)
            write_log(log_file, f"{datetime.now().isoformat()} HOOK_ERROR {name} {shlex.quote(command)} {detail}")


def send_bark(
    device_key: str,
    payload: Dict[str, Any],
    server: str = DEFAULT_BARK_SERVER,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    server = server.rstrip("/")
    if not server:
        raise BarkError("Bark server is empty")
    if not device_key:
        raise BarkError("Bark device key is empty")

    encoded_key = urllib.parse.quote(device_key, safe="")
    request = urllib.request.Request(
        f"{server}/{encoded_key}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BarkError(f"Bark HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise BarkError(f"Bark request failed: {exc.reason}") from exc

    try:
        result = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise BarkError(f"Bark returned non-JSON response: {raw_body}") from exc

    if isinstance(result, dict) and result.get("code") not in (None, 200):
        raise BarkError(f"Bark rejected request: {result}")
    return result if isinstance(result, dict) else {"response": result}


def write_log(log_file: Optional[str], message: str) -> None:
    if not log_file:
        return
    path = Path(log_file).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(message.rstrip() + "\n")
    except OSError:
        return


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a Bark push notification for Codex Stop/agent-turn-complete events.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Codex lifecycle hooks pass one JSON object on stdin.
            Legacy notify hooks pass the JSON object as the first argument.
            """
        ),
    )
    parser.add_argument("legacy_payload", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="print the Bark payload instead of sending it")
    parser.add_argument("--server", default=os.getenv("BARK_SERVER", DEFAULT_BARK_SERVER))
    parser.add_argument("--device-key", default=os.getenv("BARK_DEVICE_KEY") or os.getenv("BARK_KEY", ""))
    parser.add_argument("--title", default=os.getenv("BARK_TITLE", DEFAULT_TITLE))
    parser.add_argument("--group", default=os.getenv("BARK_GROUP", DEFAULT_GROUP))
    parser.add_argument("--sound", default=os.getenv("BARK_SOUND"))
    parser.add_argument("--url", default=os.getenv("BARK_URL"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("BARK_TIMEOUT", "10")))
    parser.add_argument("--log-file", default=os.getenv("BARK_LOG_FILE"))
    parser.add_argument(
        "--state-dir",
        default=os.getenv("CODEX_BARK_STATE_DIR", str(Path.home() / ".codex" / "codex-bark-state")),
    )
    parser.add_argument(
        "--config",
        default=os.getenv("CODEX_BARK_CONFIG", str(Path.home() / ".codex" / "codex-bark.json")),
    )
    parser.add_argument(
        "--hook-timeout",
        type=float,
        default=float(os.getenv("CODEX_BARK_HOOK_TIMEOUT", str(DEFAULT_HOOK_TIMEOUT))),
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    stdin_text = sys.stdin.read()

    try:
        event = load_event([args.legacy_payload] if args.legacy_payload else [], stdin_text)
    except ValueError as exc:
        print(json.dumps(_hook_success_output()), flush=True)
        print(f"codex-bark: {exc}", file=sys.stderr)
        return 0

    custom_config = load_custom_config(args.config)

    if event.get("hook_event_name") == "UserPromptSubmit":
        save_turn_state(event, args.state_dir)
        run_custom_hooks(custom_config, "on_user_prompt", event, None, args.hook_timeout, args.log_file)
        print(json.dumps(_hook_success_output()), flush=True)
        return 0

    if not should_notify(event):
        print(json.dumps(_hook_success_output()), flush=True)
        return 0

    state = load_turn_state(event, args.state_dir)
    payload = build_bark_payload(
        event=event,
        state=state,
        title_prefix=args.title,
        group=args.group,
        sound=args.sound,
        url=args.url,
    )

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
        return 0

    run_custom_hooks(custom_config, "before_notify", event, payload, args.hook_timeout, args.log_file)

    try:
        send_bark(args.device_key, payload, server=args.server, timeout=args.timeout)
    except BarkError as exc:
        write_log(args.log_file, f"{datetime.now().isoformat()} ERROR {exc}")
        print(json.dumps({"continue": True, "systemMessage": f"codex-bark failed: {exc}"}), flush=True)
        return 0

    write_log(args.log_file, f"{datetime.now().isoformat()} SENT {payload['title']}")
    run_custom_hooks(custom_config, "after_notify", event, payload, args.hook_timeout, args.log_file)
    print(json.dumps(_hook_success_output()), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
