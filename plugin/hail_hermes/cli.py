"""``pocket-hub`` and ``hermes hail`` CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import subprocess
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

from .protocol import DEFAULT_PORT
from .settings import DEFAULTS, read_settings, update_settings
from .childenv import minimal_env


def load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        text = Path(path).expanduser().read_text()
    except OSError as exc:
        raise SystemExit(f"Could not read config file {path}: {exc.strerror or exc}") from None
    if path.endswith(".json"):
        try:
            return json.loads(text)
        except ValueError as exc:
            raise SystemExit(f"Config file {path} is not valid JSON: {exc}") from None
    try:
        import yaml
    except ImportError:
        raise SystemExit("PyYAML is needed for YAML configs (pip install PyYAML), or use a .json config")
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"Config file {path} is not valid YAML: {exc}") from None


DEFAULT_AGENT_TOOLSETS = "clarify,pocket"


def _autostart_voice_defaults() -> dict[str, Any]:
    """Use automatic local STT (Parakeet if available, else Hermes) and TTS selection."""
    return {"stt": {"backend": "auto"}, "tts": {"backend": "auto"}}


def _apply_serve_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(cfg)
    try:
        saved = read_settings(cfg.get("state_dir"))
    except (OSError, ValueError):
        saved = dict(DEFAULTS)
    defaults = _autostart_voice_defaults()
    stt_default = saved.get("stt") or defaults["stt"]["backend"]
    tts_default = saved.get("tts") or defaults["tts"]["backend"]

    stt = cfg.get("stt")
    if "stt" not in cfg or stt is None:
        cfg["stt"] = {"backend": stt_default}
    elif isinstance(stt, str):
        cfg["stt"] = {"backend": stt}
    elif isinstance(stt, dict):
        stt = dict(stt)
        stt.setdefault("backend", stt_default)
        cfg["stt"] = stt

    tts = cfg.get("tts")
    if "tts" not in cfg or tts is None:
        cfg["tts"] = {"backend": tts_default}
    elif isinstance(tts, str):
        cfg["tts"] = {"backend": tts}
    elif isinstance(tts, dict):
        tts = dict(tts)
        tts.setdefault("backend", tts_default)
        cfg["tts"] = tts

    raw_agent = cfg.get("agent")
    agent: dict[str, Any] = dict(raw_agent) if isinstance(raw_agent, dict) else (
        {"backend": raw_agent} if isinstance(raw_agent, str) else {}
    )
    for key in ("model", "provider", "toolsets"):
        dotted = f"agent.{key}"
        if key not in agent and dotted in cfg:
            agent[key] = cfg[dotted]
        if (key not in agent or agent[key] in (None, "")) and saved.get(key):
            agent[key] = saved[key]
    if "toolsets" not in agent or agent["toolsets"] in (None, ""):
        agent["toolsets"] = DEFAULT_AGENT_TOOLSETS
    elif isinstance(agent["toolsets"], (list, tuple)):
        agent["toolsets"] = ",".join(str(t) for t in agent["toolsets"])
    cfg["agent"] = agent
    for key in ("quick_url", "quick_model", "soul_path"):
        if (key not in cfg or cfg[key] in (None, "")) and saved.get(key):
            cfg[key] = saved[key]
    return cfg


def build_hub(cfg: dict[str, Any]):
    from .agent import build_agent
    from .audio import build_stt, build_tts
    from .hub import HubConfig, PocketHub
    from .quick import QuickAgent, ensure_soul_file
    from .store import Store

    cfg = _apply_serve_defaults(cfg)
    store = Store(cfg.get("state_dir") or None)
    soul_path = ensure_soul_file(cfg.get("soul_path"), state_dir=store.dir)
    known = {f.name for f in fields(HubConfig)}
    hub_cfg = HubConfig(**{"warm_voice": True, **{k: v for k, v in cfg.items() if k in known}})
    quick_url = str(cfg.get("quick_url") or "").strip()
    quick_model = str(cfg.get("quick_model") or "").strip()
    quick_agent = (
        QuickAgent(quick_url, quick_model, soul_path=soul_path, state_dir=store.dir)
        if not quick_url or quick_model
        else None
    )
    return PocketHub(
        agent=build_agent(cfg.get("agent"), store), stt=build_stt(cfg.get("stt")),
        tts=build_tts(cfg.get("tts")), store=store, config=hub_cfg,
        quick_agent=quick_agent,
    )


def add_subcommands(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", help="hub state dir (default $HAIL_HERMES_STATE, $HERMES_POCKET_STATE, or the profile state directory)")
    sub = parser.add_subparsers(dest="pocket_cmd")
    serve = sub.add_parser("serve", help="run the hub")
    sub.add_parser("restart", help="stop the running hub and start a fresh one")
    serve.add_argument("--config", help="YAML/JSON config file (see plugin/README.md)")
    bind = serve.add_mutually_exclusive_group()
    bind.add_argument("--host")
    bind.add_argument("--lan", action="store_true", help="listen on all IPv4 interfaces")
    serve.add_argument("--port", type=int, help=f"listen port (default {DEFAULT_PORT})")
    serve.add_argument("--agent", choices=["hermes", "echo"], help="agent backend (default hermes)")
    serve.add_argument("--stt-text", help="stub STT: text returned for every utterance")
    serve.add_argument("--tts", choices=["auto", "none", "silence", "espeak", "piper", "kokoro"], help="TTS backend")
    serve.add_argument("-v", "--verbose", action="store_true")

    pair = sub.add_parser("pair", help="manage pending pairing requests")
    pair_sub = pair.add_subparsers(dest="pair_cmd")
    pair_sub.add_parser("list", help="list pending pairing requests")
    for decision in ("approve", "deny"):
        command = pair_sub.add_parser(decision, help=f"{decision} a pending pairing request")
        command.add_argument("identifier", help="four-digit match code or request ID")

    watch = sub.add_parser("watch", help="connect a Galaxy Watch using wireless debugging")
    watch.add_argument("--apk", help="APK to install on the connected watch")

    sub.add_parser("devices", help="list paired and connected devices")
    revoke = sub.add_parser("revoke", help="forget a paired device")
    revoke.add_argument("device_id")
    notify = sub.add_parser("notify", help="push a notification to a connected device")
    notify.add_argument("title")
    notify.add_argument("body")
    notify.add_argument("--device", dest="device_id")
    say = sub.add_parser("say", help="speak text on a connected device")
    say.add_argument("text")
    say.add_argument("--device", dest="device_id")
    settings_cmd = sub.add_parser("settings", help="show or update persistent Hail Hermes settings")
    settings_cmd.add_argument("key", nargs="?", help="setting key")
    settings_cmd.add_argument("value", nargs="?", help="setting value")
    sub.add_parser("models", help="list available Quick ask models")
    slots_cmd = sub.add_parser("slots", help="show or update session slots")
    slots_cmd.add_argument("slots_action", nargs="?", choices=["set", "clear"])
    slots_cmd.add_argument("slot_num", nargs="?", type=int)
    slots_cmd.add_argument("session_id", nargs="?")


def _watch(apk: str | None) -> int:
    print("1. On the Galaxy Watch, enable Developer options and Wireless debugging.")
    print("2. Choose pairing with a code on the watch, then run: adb pair <watch-ip>:<pairing-port>")
    print("3. Use the debugging address shown on the watch: adb connect <watch-ip>:<debugging-port>")
    print("4. Open the Hail Hermes app on the watch and approve its matching code in the Desktop pane.")

    adb = shutil.which("adb")
    if not adb:
        print("Install Android platform-tools to use adb, then run this command again.")
        return 0

    try:
        result = subprocess.run(
            [adb, "devices"], capture_output=True, text=True, check=True, timeout=30,
            # adb needs PATH and little else; it must not inherit provider keys (review item 2).
            env=minimal_env(),
        )
    except subprocess.TimeoutExpired:
        print("adb did not answer within 30 seconds; run `adb kill-server` and try again.", file=sys.stderr)
        return 1
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Could not list adb devices: {exc}", file=sys.stderr)
        return 1

    devices = []
    for line in result.stdout.splitlines()[1:]:
        columns = line.split()
        if len(columns) >= 2 and columns[1] == "device":
            devices.append(columns[0])
    if len(devices) != 1:
        print(f"Connect exactly one adb device before continuing; found {len(devices)}.")
        return 0

    serial = devices[0]
    if apk and not Path(apk).expanduser().is_file():
        print(f"APK not found: {apk}", file=sys.stderr)
        return 1

    try:
        if apk:
            subprocess.run(
                [adb, "-s", serial, "install", "-r", str(Path(apk).expanduser())],
                check=True, timeout=300, env=minimal_env(),
            )
        subprocess.run(
            [adb, "-s", serial, "reverse", "tcp:8767", "tcp:8767"],
            check=True, timeout=30, env=minimal_env(),
        )
    except subprocess.TimeoutExpired:
        print("adb stopped answering while preparing the watch; reconnect it and try again.", file=sys.stderr)
        return 1
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Could not prepare the connected device: {exc}", file=sys.stderr)
        return 1

    print("Watch connection prepared; approve the matching code in the Desktop pane.")
    return 0


def _lookup_cli_session_title(state_dir: str | None, session_id: str) -> str:
    from .agent import list_hermes_sessions
    from .control import ControlClient

    try:
        client = ControlClient(state_dir)
        if client.live():
            rows = client.call("sessions", limit=30)
            if isinstance(rows, list):
                for item in rows:
                    if isinstance(item, dict) and item.get("id") == session_id:
                        return str(item.get("title") or "")
    except Exception:
        pass
    try:
        for item in asyncio.run(list_hermes_sessions(limit=30)):
            if isinstance(item, dict) and item.get("id") == session_id:
                return str(item.get("title") or "")
    except Exception:
        pass
    return ""


def dispatch(args: argparse.Namespace) -> int:
    from .control import ControlClient, HubUnavailable
    from .store import Store

    cmd = getattr(args, "pocket_cmd", None)
    if cmd == "restart":
        import json as _json

        from .autostart import restart_hub

        result = restart_hub(args.state_dir)
        print(_json.dumps(result))
        return 0 if result.get("answers") else 1
    if cmd == "serve":
        cfg = load_config(args.config)
        for key, val in (("host", args.host), ("port", args.port), ("state_dir", args.state_dir)):
            if val is not None:
                cfg[key] = val
        if args.lan:
            cfg["host"] = "0.0.0.0"
        if args.agent:
            if isinstance(cfg.get("agent"), dict):
                cfg["agent"] = {**cfg["agent"], "backend": args.agent}
            else:
                cfg["agent"] = {"backend": args.agent}
        if args.stt_text:
            cfg["stt"] = {"backend": "stub", "text": args.stt_text}
        if args.tts:
            if isinstance(cfg.get("tts"), dict):
                cfg["tts"] = {**cfg["tts"], "backend": args.tts}
            else:
                cfg["tts"] = {"backend": args.tts}
        cfg = _apply_serve_defaults(cfg)
        # One hub per state directory. Two hubs bound to different interfaces both answer
        # the watch and the CLI, so killing "the" hub leaves the watch still connected to
        # the other one — which makes every recovery test silently meaningless.
        from .autostart import _answers

        if _answers(Store(cfg.get("state_dir")).dir):
            print("a hub is already answering for this state directory; not starting a second one")
            return 0
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        # Keep a log file of our own. When the desktop pane starts the hub the
        # process is detached and its stdout goes nowhere, so without this every
        # question that failed left no trace at all.
        try:
            from .store import default_state_dir

            log_dir = Path(cfg.get("state_dir") or default_state_dir())
            log_dir.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(log_dir / "hub.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            logging.getLogger().addHandler(handler)
        except OSError:
            pass
        if args.lan:
            logging.getLogger("hail_hermes.cli").warning(
                "LAN ACCESS ENABLED: Hail Hermes traffic is cleartext and can be read on the network."
            )
        hub = build_hub(cfg)
        try:
            asyncio.run(hub.serve_forever())
        except KeyboardInterrupt:
            pass
        return 0
    if cmd == "watch":
        return _watch(args.apk)
    if cmd == "pair":
        client = ControlClient(args.state_dir)
        if args.pair_cmd not in ("list", "approve", "deny"):
            print("usage: hail-hermes pair {list,approve,deny} ...", file=sys.stderr)
            return 2
        try:
            if args.pair_cmd == "list":
                pending = client.pair_list()
                if not pending:
                    print("no pending pairing requests")
                for item in pending:
                    print(
                        f"{item['match_code']}  {item['name']} ({item['kind']}, {item['device_id']})"
                        f"  from {item['remote']}  expires in {item['expires_in']:.0f}s"
                        f"  request={item['request_id']}"
                    )
            else:
                result = client.pair_decide(args.pair_cmd, args.identifier)
                verb = "approved" if args.pair_cmd == "approve" else "denied"
                print(f"{verb} pairing for {result['device_id']}")
        except (HubUnavailable, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    if cmd == "devices":
        client = ControlClient(args.state_dir)
        live = {d["device_id"]: d for d in client.connected()}
        paired = Store(args.state_dir).paired()
        for dev_id in sorted(set(paired) | set(live)):
            info = {**paired.get(dev_id, {}), **live.get(dev_id, {})}
            caps = ",".join(c for c, on in info.get("caps", {}).items() if on) or "-"
            status = "connected" if dev_id in live else "offline"
            print(f"{dev_id}  {info.get('name', '?'):<16} {info.get('kind', '?'):<5} {status:<9} caps={caps}")
        if not paired and not live:
            print("no paired devices")
        return 0
    if cmd == "revoke":
        try:
            ok = ControlClient(args.state_dir).revoke(args.device_id)["revoked"]
        except HubUnavailable:
            ok = Store(args.state_dir).revoke(args.device_id)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print("revoked" if ok else "no such device")
        return 0 if ok else 1
    if cmd in ("notify", "say"):
        client = ControlClient(args.state_dir)
        kwargs = {"title": args.title, "body": args.body} if cmd == "notify" else {"text": args.text}
        if args.device_id:
            kwargs["device_id"] = args.device_id
        try:
            print(json.dumps(client.call(cmd, **kwargs)))
        except (HubUnavailable, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    if cmd == "models":
        from .quick import list_quick_models

        saved = read_settings(args.state_dir)
        endpoints = saved.get("quick_endpoints") or []
        choices = list_quick_models(endpoints, timeout=3.0)
        if not choices:
            print("no models")
            return 0
        for item in choices:
            print(f"{item['url']}  {item['model']}")
        return 0
    if cmd == "slots":
        action = getattr(args, "slots_action", None)
        slot_num = getattr(args, "slot_num", None)
        session_id = getattr(args, "session_id", None)
        saved = read_settings(args.state_dir)
        raw_slots = list(saved.get("slots") or [])
        by_slot: dict[int, dict[str, Any]] = {}
        for idx, item in enumerate(raw_slots[:3]):
            if isinstance(item, dict) and item.get("id"):
                snum = item.get("slot") if isinstance(item.get("slot"), int) else (idx + 1)
                by_slot[snum] = {"id": str(item["id"]), "title": str(item.get("title") or "")}
        if action is None:
            if not by_slot:
                print("no slots")
                return 0
            for snum in sorted(by_slot):
                entry = by_slot[snum]
                print(f"{snum}: {entry['id']}  {entry['title']}".rstrip())
            return 0
        if slot_num not in (1, 2, 3):
            print("error: slot number must be 1, 2, or 3", file=sys.stderr)
            return 1
        if action == "set":
            if not isinstance(session_id, str) or not session_id.strip() or session_id.startswith("-"):
                print("usage: hail-hermes slots set <1|2|3> <session_id>", file=sys.stderr)
                return 2
            sid = session_id.strip()
            title = _lookup_cli_session_title(args.state_dir, sid)
            by_slot[slot_num] = {"id": sid, "title": title}
        elif action == "clear":
            by_slot.pop(slot_num, None)
        keys = sorted(by_slot)
        contiguous = keys == list(range(1, len(keys) + 1))
        new_slots: list[dict[str, Any]] = []
        for snum in keys:
            entry = dict(by_slot[snum])
            if not contiguous:
                entry["slot"] = snum
            new_slots.append(entry)
        update_settings(args.state_dir, {"slots": new_slots})
        try:
            client = ControlClient(args.state_dir)
            if client.live():
                client.call("reload_settings")
        except Exception:
            pass
        return 0
    if cmd == "settings":
        if args.key is None and args.value is None:
            for key, val in read_settings(args.state_dir).items():
                print(f"{key}={val}")
            return 0
        if args.key is None or args.value is None:
            print("usage: hail-hermes settings [key value]", file=sys.stderr)
            return 2
        parsed_val: Any = args.value
        if args.key == "stt_threads":
            try:
                parsed_val = int(args.value)
            except ValueError:
                print("error: stt_threads must be a positive integer", file=sys.stderr)
                return 1
        elif args.key in ("slots", "quick_endpoints", "watch_model_presets", "watch_can_change", "home_place", "features"):
            try:
                parsed_val = json.loads(args.value)
            except ValueError:
                parsed_val = [x.strip() for x in args.value.split(",") if x.strip()]
        elif isinstance(DEFAULTS.get(args.key), bool):
            text = args.value.strip().lower()
            if text in ("true", "yes", "on"):
                parsed_val = True
            elif text in ("false", "no", "off"):
                parsed_val = False
        try:
            merged = update_settings(args.state_dir, {args.key: parsed_val})
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for key, val in merged.items():
            print(f"{key}={val}")
        return 0
    print("usage: hail-hermes {serve,watch,pair,devices,revoke,notify,say,settings,models,slots} ...", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hail-hermes", description="Hail Hermes hub")
    add_subcommands(parser)
    return dispatch(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
