#!/usr/bin/env python3
"""Codex lifecycle hook that sends task-complete notifications via Bark."""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
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


def summarize_event(event: Dict[str, Any], title_prefix: str = DEFAULT_TITLE) -> Tuple[str, str]:
    cwd = event.get("cwd") or event.get("workspace") or ""
    cwd_name = Path(cwd).name if cwd else "unknown workspace"

    last_message = (
        event.get("last_assistant_message")
        or event.get("last-assistant-message")
        or event.get("message")
        or "Codex finished the current task."
    )
    if not isinstance(last_message, str):
        last_message = json.dumps(last_message, ensure_ascii=False)

    last_message = " ".join(last_message.split())
    if len(last_message) > MAX_BODY_CHARS:
        last_message = last_message[: MAX_BODY_CHARS - 1].rstrip() + "..."

    model = event.get("model")
    turn_id = event.get("turn_id") or event.get("turn-id")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    title = title_prefix
    body_lines = [
        f"Workspace: {cwd_name}",
        f"Time: {timestamp}",
    ]
    if model:
        body_lines.append(f"Model: {model}")
    if turn_id:
        body_lines.append(f"Turn: {turn_id}")
    body_lines.extend(["", last_message])

    return title, "\n".join(body_lines)


def build_bark_payload(
    event: Dict[str, Any],
    title_prefix: str,
    group: str,
    sound: Optional[str],
    url: Optional[str],
) -> Dict[str, Any]:
    title, body = summarize_event(event, title_prefix)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(message.rstrip() + "\n")


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

    if not should_notify(event):
        print(json.dumps(_hook_success_output()), flush=True)
        return 0

    payload = build_bark_payload(
        event=event,
        title_prefix=args.title,
        group=args.group,
        sound=args.sound,
        url=args.url,
    )

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
        return 0

    try:
        send_bark(args.device_key, payload, server=args.server, timeout=args.timeout)
    except BarkError as exc:
        write_log(args.log_file, f"{datetime.now().isoformat()} ERROR {exc}")
        print(json.dumps({"continue": True, "systemMessage": f"codex-bark failed: {exc}"}), flush=True)
        return 0

    write_log(args.log_file, f"{datetime.now().isoformat()} SENT {payload['title']}")
    print(json.dumps(_hook_success_output()), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
