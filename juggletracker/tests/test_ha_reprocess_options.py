"""Tests for the Home Assistant reprocess selector labels."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggletracker.ha_mqtt import SELECT_NONE, _build_reprocess_options  # noqa: E402


def test_selector_labels_show_attribution_but_keep_filename_mapping():
    options, option_to_filename, filename_to_option = _build_reprocess_options(
        ["clip_one.mp4", "clip_two.mp4", "clip_three.mp4"],
        {
            "clip_one.mp4": ["Artie"],
            "clip_two.mp4": ["Jessica", "Karen"],
            "clip_three.mp4": [],
        },
    )

    assert options == [
        SELECT_NONE,
        "Artie | clip_one.mp4",
        "Jessica, Karen | clip_two.mp4",
        "no juggle attempts | clip_three.mp4",
    ]
    assert option_to_filename["Artie | clip_one.mp4"] == "clip_one.mp4"
    assert filename_to_option["clip_two.mp4"] == "Jessica, Karen | clip_two.mp4"


def test_selector_marks_files_without_a_session():
    options, _, _ = _build_reprocess_options(["imported.mp4"], {})

    assert options == [SELECT_NONE, "no session | imported.mp4"]