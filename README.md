# codex-bark

Codex lifecycle hook that sends a Bark push notification when a Codex turn reaches the `Stop` event.

The hook uses Bark's public endpoint by default:

```text
https://api.day.app/{device_key}
```

It is implemented with Python's standard library and has no runtime dependencies.

## Install

From this project:

```sh
BARK_DEVICE_TOKEN='your-apns-device-token' python3 install.py
```

The installer:

- registers `BARK_DEVICE_TOKEN` with Bark when `BARK_DEVICE_KEY` is not provided
- writes `~/.codex/codex-bark.env` with the Bark device key and server URL
- enables `[features].codex_hooks = true` in `~/.codex/config.toml`
- adds a `Stop` command hook to `~/.codex/hooks.json`
- creates timestamped backups before changing existing Codex config files

The configured device key is stored in `~/.codex/codex-bark.env` with `0600` permissions.

If you already have the Bark key copied from the app, you can skip registration:

```sh
BARK_DEVICE_KEY='your-bark-key' python3 install.py
```

## Manual Test

Run a dry run without sending a push:

```sh
printf '{"hook_event_name":"Stop","cwd":"/tmp/demo","model":"gpt-test","turn_id":"t1","last_assistant_message":"done"}' \
  | python3 codex_bark.py --dry-run
```

Send a real Bark push:

```sh
printf '{"hook_event_name":"Stop","cwd":"/tmp/demo","last_assistant_message":"codex-bark test"}' \
  | BARK_DEVICE_KEY='your-device-key' python3 codex_bark.py
```

## Configuration

Environment variables read by the hook:

- `BARK_DEVICE_KEY` or `BARK_KEY`: Bark device key
- `BARK_SERVER`: Bark server, defaults to `https://api.day.app`
- `BARK_TITLE`: notification title, defaults to `Codex task complete`
- `BARK_GROUP`: Bark notification group, defaults to `Codex`
- `BARK_SOUND`: optional Bark sound
- `BARK_URL`: optional URL opened when tapping the notification
- `BARK_LOG_FILE`: optional local send log

The hook also accepts the older Codex `notify` JSON argument shape and sends notifications for `agent-turn-complete`.
