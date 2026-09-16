import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from codex_adapter import ModelSkipped
from oil_codex_title import read_json
from oil_codex_title_trigger import make_throttled_generator, trigger_state_path
from title_trigger_policy import load_trigger_config, schedule_eligible_turn


class TriggerPolicyTests(unittest.TestCase):
    def test_default_first_turn_then_every_five_new_turns(self):
        cfg = load_trigger_config({})
        self.assertEqual(cfg, {"first_trigger_turns": 1, "trigger_interval_turns": 5})
        state = {}
        outcomes = []
        for n in range(1, 12):
            state, decision = schedule_eligible_turn(state, f"turn-{n}", cfg)
            outcomes.append(decision["trigger"])
        self.assertEqual(
            outcomes,
            [True, False, False, False, False, True, False, False, False, False, True],
        )

    def test_first_then_interval(self):
        cfg = load_trigger_config({"first_trigger_turns": 2, "trigger_interval_turns": 3})
        state = {}
        outcomes = []
        for n in range(1, 9):
            state, decision = schedule_eligible_turn(state, f"turn-{n}", cfg)
            outcomes.append(decision["trigger"])
        self.assertEqual(outcomes, [False, True, False, False, True, False, False, True])

    def test_duplicate_turn_does_not_increment(self):
        cfg = load_trigger_config({"first_trigger_turns": 2, "trigger_interval_turns": 5})
        state, first = schedule_eligible_turn({}, "same", cfg)
        state2, duplicate = schedule_eligible_turn(state, "same", cfg)
        self.assertFalse(first["trigger"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(state2["eligible_turn_count"], 1)

    def test_legacy_thread_uses_interval_not_first(self):
        cfg = load_trigger_config({"first_trigger_turns": 1, "trigger_interval_turns": 3})
        state = {}
        triggers = []
        for n in range(1, 4):
            state, decision = schedule_eligible_turn(
                state,
                f"turn-{n}",
                cfg,
                legacy_already_triggered=(n == 1),
            )
            triggers.append(decision["trigger"])
        self.assertEqual(triggers, [False, False, True])

    def test_invalid_values_rejected(self):
        for value in (0, -1, 1.5, "3", 1001):
            with self.assertRaises(ValueError):
                load_trigger_config({"first_trigger_turns": value})

    def test_same_turn_retry_allowed_but_later_hook_is_duplicate(self):
        cfg = load_trigger_config({"first_trigger_turns": 1, "trigger_interval_turns": 5})
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []

            def fake_limited(binary, root_arg, config, context, *, before_model=None):
                calls.append(context)
                return {"action": "keep", "title": "", "reason": "test"}, {"input_tokens": 1}

            with patch("oil_codex_title_trigger.limited_title", side_effect=fake_limited):
                generator = make_throttled_generator(
                    "codex", root, {}, object(), "thread-1", "turn-1", cfg
                )
                generator({"attempt": 1})
                generator({"attempt": 2})
                self.assertEqual(len(calls), 2)
                self.assertEqual(
                    read_json(trigger_state_path(root, "thread-1"))["eligible_turn_count"], 1
                )

                duplicate_hook_generator = make_throttled_generator(
                    "codex", root, {}, object(), "thread-1", "turn-1", cfg
                )
                with self.assertRaises(ModelSkipped) as caught:
                    duplicate_hook_generator({"attempt": 3})
                self.assertEqual(caught.exception.status, "throttled_duplicate")
                self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
