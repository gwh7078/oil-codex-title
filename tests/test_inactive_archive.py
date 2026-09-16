import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from inactive_archive import DAY, archive_inactive_threads


OLD_ID = "11111111-1111-4111-8111-111111111111"
RECENT_ID = "22222222-2222-4222-8222-222222222222"
CURRENT_ID = "33333333-3333-4333-8333-333333333333"
SUBAGENT_ID = "44444444-4444-4444-8444-444444444444"


def thread(tid, stamp, **extra):
    return {
        "id": tid,
        "status": {"type": "idle"},
        "turns": [{"id": tid, "status": "completed", "completedAt": stamp}],
        **extra,
    }


class FakeBackend:
    def __init__(self, threads, read_sequences=None):
        self.threads = threads
        self.read_sequences = {k: list(v) for k, v in (read_sequences or {}).items()}
        self.archived = []

    def list_threads(self, *, archived=False, cwd=None):
        if archived:
            return iter([])
        return iter({"id": tid} for tid in self.threads)

    def read(self, tid):
        sequence = self.read_sequences.get(tid)
        if sequence:
            return sequence.pop(0)
        return self.threads[tid]

    def call(self, method, params):
        if method != "thread/archive":
            raise AssertionError(method)
        self.archived.append(params["threadId"])
        return {}


class InactiveArchiveTests(unittest.TestCase):
    def test_archives_only_top_level_noncurrent_threads_inactive_over_seven_days(self):
        now = 1_800_000_000
        backend = FakeBackend({
            OLD_ID: thread(OLD_ID, now - 7 * DAY - 1),
            RECENT_ID: thread(RECENT_ID, now - 7 * DAY + 1),
            CURRENT_ID: thread(CURRENT_ID, now - 30 * DAY),
            SUBAGENT_ID: thread(SUBAGENT_ID, now - 30 * DAY, parentThreadId=OLD_ID),
        })
        with TemporaryDirectory() as tmp:
            result = archive_inactive_threads(
                backend,
                Path(tmp),
                current_thread_id=CURRENT_ID,
                now=now,
                force=True,
            )

        self.assertEqual(backend.archived, [OLD_ID])
        self.assertEqual(result["archived"], [OLD_ID])

    def test_rechecks_activity_immediately_before_archive(self):
        now = 1_800_000_000
        old = thread(OLD_ID, now - 30 * DAY)
        fresh = thread(OLD_ID, now - 10)
        backend = FakeBackend(
            {OLD_ID: old},
            read_sequences={OLD_ID: [old, fresh]},
        )
        with TemporaryDirectory() as tmp:
            result = archive_inactive_threads(
                backend,
                Path(tmp),
                current_thread_id=CURRENT_ID,
                now=now,
                force=True,
            )

        self.assertEqual(backend.archived, [])
        self.assertEqual(result["counts"].get("changed_before_archive"), 1)

    def test_regular_hook_scan_is_throttled_after_a_recent_scan(self):
        now = 1_800_000_000
        backend = FakeBackend({})
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = archive_inactive_threads(
                backend,
                root,
                current_thread_id=CURRENT_ID,
                now=now,
                force=False,
            )
            second = archive_inactive_threads(
                backend,
                root,
                current_thread_id=CURRENT_ID,
                now=now + 60,
                force=False,
            )

        self.assertEqual(first["status"], "scanned")
        self.assertEqual(second["status"], "throttled")


if __name__ == "__main__":
    unittest.main()
