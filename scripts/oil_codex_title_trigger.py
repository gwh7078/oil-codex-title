#!/usr/bin/env python3
"""Throttle title updates and opportunistically archive long-inactive Codex threads."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from codex_adapter import BackendError, ModelSkipped, CodexBackend, find_codex
from inactive_archive import (
    SCAN_INTERVAL_SECONDS,
    archive_config,
    archive_inactive_threads,
    state_path as archive_state_path,
)
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
    """Create a generator invoked only after the original core skip checks pass.

    A single process_thread call may invoke the generator more than once when
    the first candidate conflicts with another title. Schedule/count the turn
    only once per closure, while still allowing those same-turn model retries.
    A later duplicate Stop Hook creates a new closure and is rejected from the
    persisted last_counted_turn_id.
    """
    counter_path = trigger_state_path(root, thread_id)
    scheduled_for_turn = None
    decision_for_turn = None

    def generate(context):
        nonlocal scheduled_for_turn, decision_for_turn

        if scheduled_for_turn is None:
            counter = read_json(counter_path)
            core_state = read_json(state_path(root, thread_id))
            scheduled_for_turn, decision_for_turn = schedule_eligible_turn(
                counter,
                turn_id,
                trigger_config,
                legacy_already_triggered=bool(
                    core_state.get("last_fingerprint") or core_state.get("last_generated_title")
                ),
            )
            if decision_for_turn["duplicate"]:
                raise ModelSkipped("throttled_duplicate")
            if not decision_for_turn["trigger"]:
                scheduled_for_turn["updated_at"] = int(time.time())
                atomic_json(counter_path, scheduled_for_turn)
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
        scheduled_for_turn["updated_at"] = int(time.time())
        atomic_json(counter_path, scheduled_for_turn)
        return candidate, usage

    return generate


def run_hook(root, config, event):
    if os.environ.get("OIL_CODEX_TITLE_WORKER") == "1":
        return {"status": "disabled"}
    if event.get("hook_event_name") != "Stop" or event.get("stop_hook_active"):
        return {"status": "ignored_event"}

    thread_id = valid_id(event["session_id"])
    turn_id = valid_id(event["turn_id"])
    binary = find_codex(config["codex_bin"])
    trigger_config = configured_trigger(root)
    with CodexBackend(binary) as backend:
        if config["enabled"]:
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
        else:
            result = {"status": "disabled"}

        # 归档和标题命名互相独立。每个 Stop Hook 都会检查是否到了扫描时间，
        # 但真正扫描最多每 6 小时一次，且永远跳过当前正在使用的话题。
        archive_result = archive_inactive_threads(
            backend,
            root,
            current_thread_id=thread_id,
        )

    if result["status"] not in ("renamed", "kept"):
        audit(root, thread_id, result)
    if archive_result["status"] == "scanned" and (
        archive_result["archived"] or archive_result["counts"].get("errors")
    ):
        audit(root, thread_id, {
            "status": "auto_archive",
            "archived_count": len(archive_result["archived"]),
            "errors": archive_result["counts"].get("errors", 0),
        })
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
    sub.add_parser("archive-scan", help="立即执行一次 7 天闲置归档扫描")
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
                "auto_archive": {
                    **archive_config(root),
                    "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
                    "state": read_json(archive_state_path(root)),
                },
            }, ensure_ascii=False))
            return 0
        if args.command == "archive-scan":
            config = load_config(root)
            binary = find_codex(config["codex_bin"])
            with CodexBackend(binary) as backend:
                result = archive_inactive_threads(
                    backend,
                    root,
                    current_thread_id=os.environ.get("CODEX_THREAD_ID"),
                    force=True,
                )
            print(json.dumps(result, ensure_ascii=False))
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
