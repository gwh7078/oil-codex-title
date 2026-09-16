"""Pure scheduling policy for throttled title evaluation."""
from __future__ import annotations

DEFAULT_TRIGGER_CONFIG = {
    "first_trigger_turns": 1,
    "trigger_interval_turns": 5,
}


def load_trigger_config(raw_config):
    config = DEFAULT_TRIGGER_CONFIG | {
        key: raw_config[key] for key in DEFAULT_TRIGGER_CONFIG if key in raw_config
    }
    for key, value in config.items():
        if type(value) is not int or not 1 <= value <= 1000:
            raise ValueError(f"{key} 必须是 1～1000 之间的整数")
    return config


def schedule_eligible_turn(state, turn_id, config, *, legacy_already_triggered=False):
    """Return (new_state, decision) without mutating input state.

    Only call this after the existing title logic has decided the turn would
    otherwise reach the naming model.
    """
    new_state = dict(state)
    if new_state.get("last_counted_turn_id") == turn_id:
        return new_state, {
            "trigger": False,
            "duplicate": True,
            "eligible_turn_count": int(new_state.get("eligible_turn_count", 0)),
        }

    count = int(new_state.get("eligible_turn_count", 0)) + 1
    if "last_trigger_count" not in new_state and legacy_already_triggered:
        # Existing installations may already have model evaluations before the
        # throttling layer is installed. Treat those as having passed "first".
        new_state["last_trigger_count"] = 0

    last_trigger = new_state.get("last_trigger_count")
    if last_trigger is None:
        trigger = count >= config["first_trigger_turns"]
        phase = "first"
    else:
        trigger = count - int(last_trigger) >= config["trigger_interval_turns"]
        phase = "interval"

    new_state["eligible_turn_count"] = count
    new_state["last_counted_turn_id"] = turn_id
    if trigger:
        new_state["last_trigger_count"] = count

    return new_state, {
        "trigger": trigger,
        "duplicate": False,
        "phase": phase,
        "eligible_turn_count": count,
    }
