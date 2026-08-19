"""The coherence judge.

Two properties carry the module, and both are about a judge being a model rather
than an oracle.

`test_a_finding_that_quotes_nothing_is_dropped` — a critique of a line nobody
wrote is the most convincing thing a bad judge produces, because it reads
exactly like a good one.

`test_a_failed_judge_is_not_a_failed_run` — the audio is unaffected by what a
judge thinks of it, so nothing about this path may take a run down.
"""

from __future__ import annotations

import json

import pytest

from kreb.render.critique import (
    AXES,
    GOOD_ENOUGH,
    Critique,
    critique,
    critique_user_prompt,
    render,
    to_json,
)
from kreb.render.narration import Narration, Segment
from test_research import ScriptedProvider, _metered


def _segment(id, text, speaker="expert"):
    return Segment(
        id=id, section_id="s1", beat_order=0, text=text,
        confidence="verified", kind="structure", speaker=speaker,
    )


def _narration(*texts):
    return Narration(
        title="T", question="Q", base_sha="abc",
        segments=tuple(
            _segment(f"n{i}", t, "host" if i % 2 == 0 else "expert")
            for i, t in enumerate(texts)
        ),
    )


SCRIPT = _narration("What happens on a retry?", "The delay doubles each time.")


def _verdict(**over):
    payload = {
        "scores": {"natural": 4, "coherent": 4, "listenable": 4},
        "findings": [],
        "summary": "Holds together.",
    }
    payload.update(over)
    return json.dumps(payload)


def _judge(narration, payload):
    return critique(narration, _metered(ScriptedProvider(payload)))


# -- the verdict ------------------------------------------------------------


def test_a_score_is_the_mean_across_axes():
    """A mean rather than a minimum: one weak axis on an otherwise good script
    is a note, not a verdict."""
    result = _judge(SCRIPT, _verdict(scores={"natural": 5, "coherent": 4, "listenable": 3}))
    assert result.ok
    assert result.score == 4.0


def test_every_axis_is_kept_alongside_the_mean():
    """Averaging hides exactly the thing worth seeing — a script that is
    coherent and lifeless scores the same as one that is neither."""
    result = _judge(SCRIPT, _verdict(scores={"natural": 1, "coherent": 5, "listenable": 3}))
    assert result.scores["natural"] == 1
    assert result.score == 3.0


def test_a_partial_verdict_is_never_good_enough():
    """From the first real run. The judge returned two of three axes and the
    mean of those two landed exactly on the threshold, so a script it had just
    called "too clean" reported as good enough. Averaging over whatever came
    back hides the gap."""
    result = _judge(SCRIPT, _verdict(scores={"natural": 2, "coherent": 4}))
    assert result.ok
    assert not result.complete
    assert result.score == 3.0
    assert not result.good_enough
    assert "did not score listenable" in result.reason


def test_a_complete_verdict_at_the_threshold_passes():
    result = _judge(SCRIPT, _verdict(scores={"natural": 3, "coherent": 3, "listenable": 3}))
    assert result.complete
    assert result.good_enough


def test_a_partial_verdict_says_so_in_the_report():
    text = render(_judge(SCRIPT, _verdict(scores={"natural": 4, "coherent": 4})))
    assert "partial" in text


def test_an_unknown_axis_is_ignored():
    result = _judge(SCRIPT, _verdict(scores={"natural": 4, "vibes": 5}))
    assert set(result.scores) <= {name for name, _ in AXES}


def test_a_score_outside_the_scale_is_clamped():
    """A judge that returns 9 has not understood the scale, and letting it
    through would make one run incomparable with every other."""
    result = _judge(SCRIPT, _verdict(scores={"natural": 9, "coherent": 0, "listenable": 3}))
    assert result.scores["natural"] == 5
    assert result.scores["coherent"] == 1


def test_a_low_score_is_reported_not_enforced():
    """The whole design decision. A bad script still produces audio; the number
    is a signal to a person, not a gate on the pipeline."""
    result = _judge(SCRIPT, _verdict(scores={"natural": 1, "coherent": 1, "listenable": 1}))
    assert result.ok
    assert not result.good_enough
    assert result.score < GOOD_ENOUGH


# -- the judge is a model, not an oracle ------------------------------------


def test_hesitation_is_not_something_the_judge_marks_down():
    """The judge has to agree with the writer about what good audio is. Scoring
    "substance" penalized filler, and filler is what keeps a conversation moving
    out loud — the judge would have pulled the script back toward the stilted
    thing the prompt stopped asking for."""
    from kreb.render.critique import CRITIQUE_SYSTEM

    assert "do not mark them down" in CRITIQUE_SYSTEM
    assert "sound human" in CRITIQUE_SYSTEM
    assert "substance" not in {name for name, _ in AXES}


def test_a_finding_that_quotes_nothing_is_dropped():
    """The most convincing thing a bad judge produces: a well-argued critique of
    a line that does not exist. It reads exactly like a good finding."""
    result = _judge(
        SCRIPT,
        _verdict(findings=[{"quote": "Great question!", "problem": "filler"}]),
    )
    assert result.findings == ()
    assert "not in the script" in result.reason


def test_a_finding_that_quotes_the_script_is_kept():
    result = _judge(
        SCRIPT,
        _verdict(findings=[{"quote": "The delay doubles each time.", "problem": "flat"}]),
    )
    assert len(result.findings) == 1
    assert result.findings[0].problem == "flat"


def test_real_findings_survive_alongside_invented_ones():
    """Dropping the whole batch because one was invented would lose the notes
    that were checkable."""
    result = _judge(
        SCRIPT,
        _verdict(findings=[
            {"quote": "The delay doubles each time.", "problem": "flat"},
            {"quote": "I made this up.", "problem": "invented"},
        ]),
    )
    assert len(result.findings) == 1
    assert "1 finding" in result.reason


def test_the_judge_never_sees_the_document():
    """A judge that could see the source could reward a script for being
    faithful to it — which is Gate A's job, done with anchors rather than
    opinions."""
    prompt = critique_user_prompt(SCRIPT)
    assert "HOST:" in prompt and "EXPERT:" in prompt
    assert "confidence" not in prompt
    assert "verified" not in prompt


def test_the_speakers_are_labelled_for_the_judge():
    """Without labels there is no way to tell a two-person script from one
    person talking, which is the first thing being judged."""
    assert critique_user_prompt(SCRIPT).startswith("HOST: What happens on a retry?")


# -- failure is never fatal -------------------------------------------------


def test_a_failed_judge_is_not_a_failed_run():
    """The audio is unaffected by what a judge thinks of it."""
    result = _judge(SCRIPT, "not json at all")
    assert not result.ok
    assert result.reason
    assert result.score == 0.0


def test_a_judge_returning_no_scores_says_so():
    result = _judge(SCRIPT, json.dumps({"findings": [], "summary": "ok"}))
    assert not result.ok
    assert "no scores" in result.reason


def test_a_provider_that_raises_is_caught():
    class Exploding:
        def complete(self, request):
            raise RuntimeError("upstream is down")

    result = critique(SCRIPT, _metered(Exploding()))
    assert not result.ok
    assert "upstream is down" in result.reason


def test_a_budget_ceiling_stops_the_judge_without_raising():
    """The cheapest call in the run should not be the one that takes it down."""
    from kreb.budget.policy import Budget

    provider = _metered(ScriptedProvider(_verdict()))
    provider.budget = Budget(max_per_run=0.0)
    result = critique(SCRIPT, provider)
    assert not result.ok
    assert "not judged" in result.reason


# -- the revision note ------------------------------------------------------


def test_the_revision_note_carries_only_quoted_lines():
    """The whole reason this is allowed to exist. `research/writer.py` forbids
    semantic rejection reasons, and a quoted finding is a step away from that:
    it points at a line verifiably in the script. Closer to "this symbol does
    not resolve" than to "this reads badly"."""
    from kreb.render.critique import revision_note

    result = _judge(SCRIPT, _verdict(
        scores={"natural": 2, "coherent": 3, "listenable": 2},
        summary="Sounds like a quiz throughout.",
        findings=[{"quote": "The delay doubles each time.", "problem": "flat"}],
    ))
    note = revision_note(result)
    assert "The delay doubles each time." in note
    assert "flat" in note


def test_the_revision_note_hides_the_score_and_the_summary():
    """A model told it scored 2 out of 5 on "natural" has a target with no text
    attached, and the cheapest way to move that number is to write something
    blander no judge objects to."""
    from kreb.render.critique import revision_note

    result = _judge(SCRIPT, _verdict(
        scores={"natural": 2, "coherent": 3, "listenable": 2},
        summary="Sounds like a quiz throughout.",
        findings=[{"quote": "The delay doubles each time.", "problem": "flat"}],
    ))
    note = revision_note(result)
    assert "quiz" not in note
    assert "2" not in note.replace("2 out of", "")


def test_the_revision_note_warns_against_playing_safe():
    """Models revise toward safe. A blander script that no judge objects to is
    not an improvement, and the note has to say so."""
    from kreb.render.critique import revision_note

    result = _judge(SCRIPT, _verdict(
        findings=[{"quote": "The delay doubles each time.", "problem": "flat"}]))
    assert "safer script is not a better one" in revision_note(result)


def test_no_findings_means_no_revision():
    """Nothing to point at is nothing to fix, and a revision prompted by an
    empty note is a second generation bought for nothing."""
    from kreb.render.critique import revision_note

    assert revision_note(_judge(SCRIPT, _verdict())) == ""


def test_an_invented_finding_never_reaches_the_revision():
    """Dropped upstream, so the writer is never handed a note about a line that
    does not exist."""
    from kreb.render.critique import revision_note

    result = _judge(SCRIPT, _verdict(
        findings=[{"quote": "Great question!", "problem": "filler"}]))
    assert revision_note(result) == ""


# -- the report -------------------------------------------------------------


def test_the_attempt_is_charged_even_when_unreadable():
    """A judge that answered nonsense still burned tokens, and spend that does
    not reach the ledger is spend nobody sees."""
    provider = _metered(ScriptedProvider("not json"))
    critique(SCRIPT, provider)
    assert provider.ledger.total(phase=provider.phase) > 0


def test_the_json_carries_the_threshold_it_was_judged_against():
    """A score without its threshold is unreadable a month later."""
    payload = json.loads(to_json(_judge(SCRIPT, _verdict())))
    assert payload["threshold"] == GOOD_ENOUGH
    assert payload["good_enough"] is True


def test_the_rendered_report_names_every_axis():
    text = render(_judge(SCRIPT, _verdict()))
    for name, _ in AXES:
        assert name in text


def test_an_unjudged_script_renders_its_reason():
    assert "not judged" in render(Critique(reason="not judged: no key"))
