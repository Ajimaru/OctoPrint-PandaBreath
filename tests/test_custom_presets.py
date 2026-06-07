"""Tests for user-defined custom drying presets.

Covers the pure ``validate_custom_preset`` helper plus the plugin's
``_save_custom_preset`` / ``_delete_custom_preset`` settings logic. Both are
duck-typed over ``self._settings`` so they test without a live OctoPrint
harness, the same way ``test_frame_log_gate`` does.
"""

import logging
from typing import Any, cast

import pytest

from octoprint_pandabreath import (
    CUSTOM_PRESET_MAX,
    PandabreathPlugin,
    validate_custom_preset,
)

# ---- validate_custom_preset --------------------------------------------


def test_validate_ok_normalises():
    p = validate_custom_preset("  PLA Trocken ", "45", "8")
    assert p == {"name": "PLA Trocken", "target": 45.0, "hours": 8}


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "<script>alert(1)</script>",
        "bad/name",
        "emoji \U0001f600",
        "x" * 33,  # too long
    ],
)
def test_validate_rejects_bad_name(name):
    with pytest.raises(ValueError):
        validate_custom_preset(name, 45, 8)


def test_validate_allows_accented_and_punctuation():
    p = validate_custom_preset("PLA_für-Müll 2", 50, 10)
    assert p["name"] == "PLA_für-Müll 2"


@pytest.mark.parametrize("target", [39.9, 60.1, "nan-ish", None])
def test_validate_rejects_out_of_range_target(target):
    with pytest.raises(ValueError):
        validate_custom_preset("ok", target, 8)


@pytest.mark.parametrize("hours", [0, 100, "x"])
def test_validate_rejects_out_of_range_hours(hours):
    with pytest.raises(ValueError):
        validate_custom_preset("ok", 45, hours)


# ---- save / delete via plugin settings ----------------------------------


class FakeSettings:
    """In-memory stand-in supporting get/set/save for the preset store."""

    def __init__(self, presets=None):
        self._data = {"custom_presets": list(presets or [])}
        self.saved = 0

    def get(self, path):
        return self._data.get(path[0])

    def set(self, path, value):
        self._data[path[0]] = value

    def save(self):
        self.saved += 1


def _plugin(presets=None):
    plugin = cast(Any, object.__new__(PandabreathPlugin))
    # pylint: disable=protected-access
    plugin._logger = logging.getLogger("test")
    plugin._settings = FakeSettings(presets)
    return plugin


def test_save_appends_new_preset():
    plugin = _plugin()
    result = plugin._save_custom_preset("PLA", 45, 8)
    assert result == [{"name": "PLA", "target": 45.0, "hours": 8}]
    assert plugin._settings.saved == 1


def test_save_replaces_same_name_in_place():
    plugin = _plugin([{"name": "PLA", "target": 45.0, "hours": 8}])
    result = plugin._save_custom_preset("PLA", 50, 12)
    assert result == [{"name": "PLA", "target": 50.0, "hours": 12}]
    assert len(result) == 1  # replaced, not duplicated


def test_save_rejects_over_max():
    presets = [
        {"name": f"p{i}", "target": 45.0, "hours": 8} for i in range(CUSTOM_PRESET_MAX)
    ]
    plugin = _plugin(presets)
    with pytest.raises(ValueError):
        plugin._save_custom_preset("one-too-many", 45, 8)


def test_save_rejects_invalid_without_persisting():
    plugin = _plugin()
    with pytest.raises(ValueError):
        plugin._save_custom_preset("bad/name", 45, 8)
    assert plugin._settings.saved == 0
    assert plugin._settings.get(["custom_presets"]) == []


def test_delete_removes_named_preset():
    plugin = _plugin(
        [
            {"name": "PLA", "target": 45.0, "hours": 8},
            {"name": "PETG", "target": 55.0, "hours": 10},
        ]
    )
    result = plugin._delete_custom_preset("PLA")
    assert result == [{"name": "PETG", "target": 55.0, "hours": 10}]


def test_delete_unknown_is_noop():
    plugin = _plugin([{"name": "PLA", "target": 45.0, "hours": 8}])
    result = plugin._delete_custom_preset("nope")
    assert result == [{"name": "PLA", "target": 45.0, "hours": 8}]
