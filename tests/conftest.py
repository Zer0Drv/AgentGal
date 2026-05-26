"""Shared test helpers available to all tests in this directory."""

from agents.schema import NarratorOutput


def _narrator_output(**overrides) -> NarratorOutput:
    data = {
        "targets": ["mitsuki"],
        "date": "4月3日 星期三",
        "time": "16:10",
        "location": "走廊",
        "present_characters": {"北原悠": "门口", "美月": "窗边"},
        "scene_description": "走廊里传来广播声。",
        "character_locations": {"北原悠": "走廊", "美月": "走廊"},
        "new_characters": [],
    }
    data.update(overrides)
    return NarratorOutput(**data)
