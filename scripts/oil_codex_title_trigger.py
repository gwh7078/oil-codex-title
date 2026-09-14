#!/usr/bin/env python3
"""Throttle the existing oil-codex-title Stop hook without changing its skip rules."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from codex_adapter import BackendError, ModelSkipped, CodexBackend, find_codex
from oil_codex_title import (
    atomic_json,
    audit,
    data_dir,
    ensure_title_active,
    limited_title,
    load_config,
    process_thread,
    read_json,
    state_path,
    valid_id,
)
from title_trigger_policy import load_trigger_config, schedule_eligible_turn


def trigger_state_path(root: Path, thread_id: str) -> Path:
    return root / "trigger-counters" / f"{thread_id}.json"


def configured_trigger(root: Path):
    return load_trigger_config(read_json(root / "config.json"))


def configure_trigger(root: Path, *, first=None, interval=None):
    path = root / "config.json"
    raw = read_json(path)
    if first is not None:
        raw["first_trigger_turns"] = first
    if interval is not None:
        raw["trigger_interval_turns"] = interval
    settings = load_trigger_config(raw)
    raw.update(settings)
    atomic_json(path, raw)
    return settings


def make_throttled_generator(binary, root, config, backend, thread_id, turn_id, trigger_config):
    """Create a generator invoked only after the original core skip checks pass."""
    counter_path = trigger_state_path(root, thread_id)

    def generate(context):
        counter = read_json(counter_path)
        core_state = read_json(state_path(root, thread_id))
        scheduled, decision = schedule_eligible_turn(
            counter,
            turn_id,
            trigger_config,
            legacy_already_triggered=bool(
                core_state.get("last_fingerprint") or core_state.get("last_generated_title")
            ),
        )
        if decision["duplicate"]:
            raise ModelSkipped("throttled_duplicate")
        if not decision["trigger"]:
            scheduled["updated_at"] = int(time.time())
            atomic_json(counter_path, scheduled)
            raise ModelSkipped("throttled")

        # Dynamic skips while waiting for a worker slot (pause/archive/lock)
        # do not consume this trigger because the counter is persisted only
        # after the model call successfully returns.
        candidate, usage = limited_title(
            binary,
            root,
            config,
            context,
            before_model=lambda: ensure_title_active(backend, thread_id, root),
        )
        scheduled["updated_at"] = int(time.time())
        atomic_json(counter_path, scheduled)
        return candidate, usage

    return generate


def run_hook(root, config, event):
    if os.environ.get("OIL_CODEX_TITLE_WORKER") == "1" or not config["enabled"]:
        return {"status": "disabled"}
    if event.get("hook_event_name") != "Stop" or event.get("stop_hook_active"):
        return {"status": "ignored_event"}

    thread_id = valid_id(event["session_id"])
    turn_id = valid_id(event["turn_id"])
    binary = find_codex(config["codex_bin"])
    trigger_config = configured_trigger(root)
    with CodexBackend(binary) as backend:
        result = process_thread(
            backend,
            make_throttled_generator(
                binary, root, config, backend, thread_id, turn_id, trigger_config
            ),
            thread_id,
            root,
            config,
            apply=True,
            event_turn=turn_id,
        )
    if result["status"] not in ("renamed", "kept"):
        audit(root, thread_id, result)
    return result


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="oil-codex-title 可配置轮次触发层")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook")
    p = sub.add_parser("configure")
    p.add_argument("--first-trigger-turns", type=int)
    p.add_argument("--trigger-interval-turns", type=int)
    sub.add_parser("status")
    args = parser.parse_args()

    root = data_dir()
    is_hook = args.command == "hook"
    thread_id = None
    try:
        if args.command == "configure":
            settings = configure_trigger(
                root,
                first=args.first_trigger_turns,
                interval=args.trigger_interval_turns,
            )
            print(json.dumps(settings, ensure_ascii=False))
            return 0
        if args.command == "status":
            print(json.dumps({
                "trigger": configured_trigger(root),
                "tracked_counters": len(list((root / "trigger-counters").glob("*.json"))),
            }, ensure_ascii=False))
            return 0

        config = load_config(root)
        event = json.loads(sys.stdin.read(1024 * 1024))
        thread_id = event.get("session_id")
        run_hook(root, config, event)
        return 0
    except Exception as exc:
        if is_hook:
            try:
                audit(root, valid_id(thread_id) if thread_id else "hook", {
                    "status": "error", "error_type": type(exc).__name__
                })
            except Exception:
                pass
            return 0
        message = str(exc) if isinstance(exc, (BackendError, ValueError)) else "操作失败；检查输入及本地配置"
        print(json.dumps({
            "status": "error",
            "error_type": type(exc).__name__,
            "message": message,
        }, ensure_ascii=False))
        return 1
    finally:
        if is_hook:
            print("{}")


if __name__ == "__main__":
    raise SystemExit(main())
