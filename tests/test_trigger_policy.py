import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from title_trigger_policy import load_trigger_config, schedule_eligible_turn


class TriggerPolicyTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
