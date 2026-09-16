"""轻量闲置归档：仅按最后一次已完成对话时间判断，不调用模型。"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import time

from codex_adapter import BackendError
from oil_codex_title import atomic_json, read_json, thread_lock, valid_id

DAY = 86400
DEFAULT_ARCHIVE_DAYS = 7
SCAN_INTERVAL_SECONDS = 6 * 60 * 60


def archive_config(root: Path) -> dict:
    raw = read_json(root / "config.json")
    enabled = raw.get("auto_archive_enabled", True)
    days = raw.get("auto_archive_days", DEFAULT_ARCHIVE_DAYS)
    if type(enabled) is not bool:
        raise ValueError("auto_archive_enabled 必须是布尔值")
    if type(days) is not int or not 1 <= days <= 365:
        raise ValueError("auto_archive_days 必须是 1～365 的整数")
    return {"enabled": enabled, "days": days}


def state_path(root: Path) -> Path:
    return root / "auto-archive" / "state.json"


def latest_activity(thread: dict):
    """返回最后一个已完成轮次的 ID 和时间；标题/元数据变化不重置闲置计时。"""
    turns = thread.get("turns") or []
    if not turns:
        return None
    latest = turns[-1]
    if latest.get("status") != "completed":
        return None
    stamp = latest.get("completedAt") or latest.get("startedAt")
    if type(stamp) not in (int, float) or stamp <= 0:
        return None
    return latest.get("id"), stamp


def is_top_level_idle_thread(thread: dict) -> bool:
    if thread.get("ephemeral") or thread.get("parentThreadId"):
        return False
    status = thread.get("status")
    if isinstance(status, dict) and status.get("type") not in (None, "idle", "notLoaded"):
        return False
    return True


def archive_inactive_threads(
    backend,
    root: Path,
    *,
    current_thread_id: str | None = None,
    now: float | None = None,
    force: bool = False,
):
    """归档超过阈值无新对话的普通话题；归档前二次读取以避免竞态。"""
    now = time.time() if now is None else now
    cfg = archive_config(root)
    if not cfg["enabled"]:
        return {"status": "disabled", "archived": [], "counts": {}}

    current = valid_id(current_thread_id) if current_thread_id else None
    lock_root = root / "auto-archive"
    with thread_lock(lock_root, "scan") as acquired:
        if not acquired:
            return {"status": "busy", "archived": [], "counts": {}}

        path = state_path(root)
        state = read_json(path)
        last_scan_at = state.get("last_scan_at")
        if (
            not force
            and type(last_scan_at) in (int, float)
            and 0 <= now - last_scan_at < SCAN_INTERVAL_SECONDS
        ):
            return {"status": "throttled", "archived": [], "counts": {}}

        cutoff = now - cfg["days"] * DAY
        archived = []
        counts = Counter()

        for meta in backend.list_threads(archived=False):
            tid = valid_id(meta["id"])
            counts["listed"] += 1
            if tid == current:
                counts["current"] += 1
                continue

            try:
                first = backend.read(tid)
                if not is_top_level_idle_thread(first):
                    counts["protected"] += 1
                    continue
                first_activity = latest_activity(first)
                if first_activity is None:
                    counts["unknown_activity"] += 1
                    continue
                if first_activity[1] > cutoff:
                    counts["recent"] += 1
                    continue

                # 归档前立即再读一次；只要轮次或时间发生变化就保留，下一次扫描再判断。
                fresh = backend.read(tid)
                fresh_activity = latest_activity(fresh)
                if (
                    not is_top_level_idle_thread(fresh)
                    or fresh_activity is None
                    or fresh_activity != first_activity
                    or fresh_activity[1] > cutoff
                ):
                    counts["changed_before_archive"] += 1
                    continue

                backend.call("thread/archive", {"threadId": tid})
                archived.append(tid)
                counts["archived"] += 1
            except BackendError:
                counts["errors"] += 1

        atomic_json(path, {
            "last_scan_at": now,
            "archive_days": cfg["days"],
            "archived_count": len(archived),
        })
        return {"status": "scanned", "archived": archived, "counts": dict(counts)}
