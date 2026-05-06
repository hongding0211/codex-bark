#!/usr/bin/env python3
"""Install codex-bark into the user's Codex lifecycle hook config."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import stat
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


PROJECT_ROOT = Path(__file__).resolve().parent
HOOK_SCRIPT = PROJECT_ROOT / "codex_bark.py"


def backup(path: Path) -> None:
    if not path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copy2(path, path.with_name(f"{path.name}.codex-bark.{stamp}.bak"))


def ensure_codex_hooks_feature(config_path: Path) -> bool:
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    lines = original.splitlines()

    feature_header_index = None
    for index, line in enumerate(lines):
        if line.strip() == "[features]":
            feature_header_index = index
            break

    if feature_header_index is None:
        updated = original.rstrip() + "\n\n[features]\ncodex_hooks = true\n"
        config_path.write_text(updated.lstrip("\n"), encoding="utf-8")
        return updated != original

    table_end = len(lines)
    for index in range(feature_header_index + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            table_end = index
            break

    for index in range(feature_header_index + 1, table_end):
        stripped = lines[index].strip()
        if stripped.startswith("codex_hooks"):
            if stripped == "codex_hooks = true":
                return False
            lines[index] = "codex_hooks = true"
            config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True

    lines.insert(feature_header_index + 1, "codex_hooks = true")
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def load_hooks(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"hooks": {}}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path} has invalid hooks shape")
    return data


def install_stop_hook(hooks_path: Path, env_path: Path, log_path: Path) -> bool:
    data = load_hooks(hooks_path)
    stop_hooks = data.setdefault("hooks", {}).setdefault("Stop", [])
    if not isinstance(stop_hooks, list):
        raise ValueError("hooks.Stop must be an array")

    command = (
        "/bin/zsh -lc "
        + repr(
            f"set -a; [ -f {str(env_path)!r} ] && source {str(env_path)!r}; "
            f"/usr/bin/python3 {str(HOOK_SCRIPT)!r}"
        )
    )
    entry = {
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": 20,
                "statusMessage": "Sending Bark notification",
            }
        ]
    }

    marker = str(HOOK_SCRIPT)
    filtered = []
    for group in stop_hooks:
        group_text = json.dumps(group, ensure_ascii=False)
        if marker not in group_text:
            filtered.append(group)
    filtered.append(entry)
    data["hooks"]["Stop"] = filtered

    new_text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    old_text = hooks_path.read_text(encoding="utf-8") if hooks_path.exists() else ""
    if new_text == old_text:
        return False
    hooks_path.write_text(new_text, encoding="utf-8")
    return True


def write_env(env_path: Path, device_key: str, server: str, log_path: Path) -> bool:
    def line(name: str, value: str) -> str:
        return f"{name}={shlex.quote(str(value))}"

    content = "\n".join(
        [
            line("BARK_DEVICE_KEY", device_key),
            line("BARK_SERVER", server),
            line("BARK_TITLE", "Codex task complete"),
            line("BARK_GROUP", "Codex"),
            line("BARK_LOG_FILE", str(log_path)),
            "",
        ]
    )
    old = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if old == content:
        return False
    env_path.write_text(content, encoding="utf-8")
    env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return True


def register_device_token(server: str, device_token: str, requested_key: str = "") -> str:
    payload = {"devicetoken": device_token}
    if requested_key:
        payload["key"] = requested_key

    request = urllib.request.Request(
        server.rstrip("/") + "/register",
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Bark register HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Bark register failed: {exc.reason}") from exc

    result = json.loads(raw_body)
    if result.get("code") != 200:
        raise RuntimeError(f"Bark register rejected request: {result}")

    data = result.get("data") or {}
    device_key = data.get("device_key") or data.get("key")
    if not device_key:
        raise RuntimeError(f"Bark register response did not include device_key: {result}")
    return str(device_key)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install codex-bark as a Codex Stop lifecycle hook.")
    parser.add_argument("--codex-home", default=os.getenv("CODEX_HOME", str(Path.home() / ".codex")))
    parser.add_argument("--server", default=os.getenv("BARK_SERVER", "https://api.day.app"))
    parser.add_argument("--device-key", default=os.getenv("BARK_DEVICE_KEY", ""))
    parser.add_argument("--device-token", default=os.getenv("BARK_DEVICE_TOKEN", ""))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    codex_home = Path(args.codex_home).expanduser()
    codex_home.mkdir(parents=True, exist_ok=True)

    config_path = codex_home / "config.toml"
    hooks_path = codex_home / "hooks.json"
    env_path = codex_home / "codex-bark.env"
    log_path = codex_home / "codex-bark.log"

    backup(config_path)
    backup(hooks_path)

    device_key = args.device_key
    if not device_key:
        if not args.device_token:
            raise SystemExit("Provide --device-key or --device-token, or set BARK_DEVICE_KEY/BARK_DEVICE_TOKEN.")
        device_key = register_device_token(args.server, args.device_token)

    env_changed = write_env(env_path, device_key, args.server, log_path)
    config_changed = ensure_codex_hooks_feature(config_path)
    hooks_changed = install_stop_hook(hooks_path, env_path, log_path)

    print(f"Codex home: {codex_home}")
    print(f"Env file: {env_path} {'updated' if env_changed else 'unchanged'}")
    print(f"Config: {config_path} {'updated' if config_changed else 'unchanged'}")
    print(f"Hooks: {hooks_path} {'updated' if hooks_changed else 'unchanged'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
