"""Presets: how long the audio runs, and how far in it goes.

The load-bearing fact is that `beats` decides length, not narration. The first
real run produced 8 beats, 784 words, 4.8 minutes — so a forty-minute target is
not a chattier narrator, it is roughly eight times the beats. A preset that
raised the word target without raising the beat count would produce eight padded
beats, which is the same script read slower.
"""

from __future__ import annotations

import pytest

from kreb.render.shape import (
    DEFAULT,
    PRESETS,
    WORDS_PER_MINUTE,
    brief,
    shape_for,
)


def test_the_words_per_minute_is_measured_not_guessed():
    """784 words over 286.32 seconds of real Flux TTS output. A guessed rate
    would make every minute target quietly wrong."""
    assert 150 < WORDS_PER_MINUTE < 180


def test_quick_matches_the_run_that_calibrated_it():
    """8 beats and ~5 minutes is exactly what the real run produced, which is
    the only reason the other two presets can be trusted to scale."""
    quick = shape_for("quick")
    assert quick.beats == 8
    assert quick.minutes == 5


def test_a_longer_preset_plans_more_beats():
    """The whole point. Length has to reach back to what gets planned, or a
    forty-minute target is eight beats padded out."""
    assert shape_for("deep").beats > shape_for("standard").beats > shape_for("quick").beats


def test_deep_is_long_enough_to_be_worth_asking_for():
    deep = shape_for("deep")
    assert deep.minutes == 40
    assert deep.words > 6000
    assert deep.beats > 60


def test_every_preset_names_an_audience_and_a_depth():
    """A word count alone changes length without changing register, and a long
    script at overview depth is a short script repeating itself."""
    for shape in PRESETS.values():
        assert shape.audience and shape.detail
        assert shape.depth


def test_the_default_is_the_middle_one():
    assert shape_for(DEFAULT).minutes == 15


def test_an_unknown_preset_names_the_real_ones():
    with pytest.raises(ValueError) as caught:
        shape_for("epic")
    for name in PRESETS:
        assert name in str(caught.value)


def test_the_brief_states_the_target_as_a_target():
    """A hard word count invites padding or truncation to hit it, and both are
    worse than a script that runs long because the material was there."""
    text = brief(shape_for("deep"))
    assert "roughly" in text
    assert "not a limit" in text
    assert "6,560" in text


def test_the_brief_carries_the_audience():
    assert "working in this codebase next week" in brief(shape_for("deep"))
    assert "worth their afternoon" in brief(shape_for("quick"))


def test_the_beat_target_reaches_the_planner_prompt():
    """Where length is actually decided. A preset that changed the narration
    prompt but not the beat plan would ask for forty minutes over eight beats,
    which is eight padded beats — the same script read slower."""
    from kreb.doc.schema import Capabilities, Document, Section
    from kreb.render.beats import beats_user_prompt

    doc = Document(
        title="T", question="Q",
        capabilities=Capabilities(base_sha="abc"),
        sections=(Section(id="s1", title="A", kind="structure", body="B",
                          confidence="derived"),),
    )
    deep = beats_user_prompt(doc, shape_for("deep"))
    assert "67 beats" in deep
    assert "40 minutes" in deep
    # And a caller with no shape gets no target rather than a stale one.
    assert "67 beats" not in beats_user_prompt(doc)


def test_a_beat_count_never_collapses_to_nothing():
    """`max(4, ...)` — a preset someone edits down to one minute should still
    produce a script rather than an empty plan."""
    from kreb.render.shape import Shape

    tiny = Shape(name="t", minutes=1, depth="overview", audience="a", detail="b")
    assert tiny.beats >= 4
