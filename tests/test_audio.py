"""Tests for beats, narration and the audio chain.

Two of these are the ones that rot silently if nobody watches them.

`test_a_speculative_beat_must_be_hedged_out_loud` guards the only *semantic*
property in the pipeline that is mechanically enforceable — and it is only
enforceable because it is stated positively. Turn it into "must not sound
overconfident" and it becomes unenforceable overnight while still looking like a
rule.

`test_the_cache_key_changes_when_the_voice_changes` guards a failure with no
symptom: upgrade piper, edit one paragraph, and one segment comes back in a
different timbre while every artifact hash reports a clean run.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from kreb.doc.schema import Anchor, Capabilities, Document, Section
from kreb.index.repo_index import build_index
from kreb.media import check_tools, duration_of
from kreb.render.audio import build_audio, segment_key, timings_json
from kreb.render.beats import (
    Beat,
    BeatsPlan,
    allowed_symbols,
    document_digest,
    plan_beats,
    unlicensed_symbols,
)
from kreb.render.beats import from_json as beats_from_json
from kreb.render.beats import to_json as beats_to_json
from kreb.render.narration import (
    BACKGROUND_PREFIX,
    MAX_SENTENCES,
    Narration,
    Segment,
    has_hedge,
    speakable,
    write_narration,
)
from kreb.repo.access import Repository
from kreb.tts.base import Spoken
from kreb.tts.piper import PiperEngine
from kreb.tts.silence import SilenceEngine
from test_research import ScriptedProvider, _metered

_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}

SOURCE = """\
MAX_RETRIES = 5


def backoff_seconds(attempt):
    return min(2 ** attempt, 60)


def unrelated_helper(x):
    return x
"""

has_ffmpeg = pytest.mark.skipif(not check_tools(), reason="ffmpeg/ffprobe not installed")


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True, env=_ENV)
    (root / "retry.py").write_text(SOURCE)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, env=_ENV)
    subprocess.run(["git", "commit", "-q", "-m", "f"], cwd=root, check=True, env=_ENV)
    return Repository(root)


@pytest.fixture()
def index(repo):
    return build_index(repo)


def _section(index, *, id="s1", confidence="derived", kind="structure", body="Prose."):
    symbol = index.symbols["retry.py#backoff_seconds"]
    return Section(
        id=id,
        title="Backoff",
        kind=kind,
        body=body,
        confidence=confidence,
        anchors=(Anchor(ref=symbol.ref, text_hash=symbol.text_hash, lines=(4, 5)),),
    )


def _doc(index, *sections, **caps):
    return Document(
        title="How retries work",
        question="How does backoff work?",
        capabilities=Capabilities(base_sha=index.sha, **caps),
        sections=tuple(sections),
    )


def _beats(*items):
    return json.dumps({"beats": [{"section_id": s, "key_point": p} for s, p in items]})


def _lines(*items):
    return json.dumps({"lines": [{"order": o, "text": t} for o, t in items]})


def _plan(*beats):
    return BeatsPlan(title="T", question="Q", base_sha="abc", beats=tuple(beats))


def _beat(order=0, *, confidence="derived", kind="structure", point="The delay doubles."):
    return Beat(
        section_id="s1",
        section_title="Backoff",
        key_point=point,
        confidence=confidence,
        kind=kind,
        order=order,
    )


def _seed(out, doc, *, text="The delay doubles."):
    """Write a beats/narration pair that legitimately belongs to `doc`."""
    plan = _plan(_beat(point=text))
    plan.doc_digest = document_digest(doc)
    (out / "beats.json").write_text(beats_to_json(plan))
    (out / "narration.json").write_text(
        json.dumps(
            {
                "title": "T", "question": "", "base_sha": "abc",
                "segments": [
                    {"id": "n000", "section_id": "s1", "beat_order": 0, "text": text,
                     "confidence": "derived", "kind": "structure", "role": "beat"}
                ],
            }
        )
    )


# -- the flags are derived, never authored ----------------------------------


def test_a_beat_carries_no_settable_flag():
    """`hedge_required` as a field would be settable — by a model returning it,
    by a caller building a Beat from a dict, by a renderer that found it
    inconvenient. The hedge validator downstream only means something if the
    requirement is recomputed from the section every time."""
    fields = set(Beat.__dataclass_fields__)
    assert "hedge_required" not in fields
    assert "prefix_required" not in fields
    assert _beat(confidence="speculative").hedge_required is True
    assert _beat(confidence="verified").hedge_required is False


def test_the_model_cannot_set_confidence_on_a_beat(index, repo):
    """It comes off the section, so a model cannot upgrade its own hedge away."""
    doc = _doc(index, _section(index, confidence="speculative"))
    provider = ScriptedProvider(_beats(("s1", "The delay doubles each attempt.")))
    result = plan_beats(doc, index, _metered(provider))
    assert result.ok
    assert result.plan.beats[0].confidence == "speculative"
    assert result.plan.beats[0].hedge_required is True


def test_a_serialized_plan_recomputes_its_flags_on_the_way_back():
    plan = _plan(_beat(confidence="speculative"))
    text = beats_to_json(plan)
    assert '"hedge_required": true' in text  # written for a reader
    tampered = json.loads(text)
    tampered["beats"][0]["hedge_required"] = False
    assert beats_from_json(json.dumps(tampered)).beats[0].hedge_required is True


# -- what beats must cover --------------------------------------------------


def test_a_section_with_no_beat_is_rejected(index, repo):
    """A model that quietly drops the two sections it found hardest to
    summarize produces audio that is shorter, smoother and missing the part
    you wanted."""
    doc = _doc(index, _section(index, id="s1"), _section(index, id="s2"))
    provider = ScriptedProvider(
        _beats(("s1", "The delay doubles.")),
        _beats(("s1", "The delay doubles."), ("s2", "It is capped.")),
    )
    result = plan_beats(doc, index, _metered(provider))
    assert result.ok
    assert result.attempts == 2
    assert any("s2" in r for r in result.rejections)


def test_a_beat_naming_an_unknown_section_is_rejected(index, repo):
    doc = _doc(index, _section(index))
    provider = ScriptedProvider(_beats(("s9", "Something.")), _beats(("s1", "Something.")))
    result = plan_beats(doc, index, _metered(provider))
    assert result.ok
    assert any("`s9`" in r for r in result.rejections)


def test_a_beat_may_not_name_a_symbol_its_section_never_cited(index, repo):
    """Set containment over the index, which is why this absence check is
    legitimate where 'does not sound like documentation' is not."""
    section = _section(index)
    assert "backoff_seconds" in allowed_symbols(section, index)
    assert unlicensed_symbols("It calls `unrelated_helper`.", section, index) == {
        "unrelated_helper"
    }
    assert unlicensed_symbols("It calls `backoff_seconds`.", section, index) == set()


def test_an_unlicensed_symbol_forces_a_retry(index, repo):
    doc = _doc(index, _section(index))
    provider = ScriptedProvider(
        _beats(("s1", "It delegates to `unrelated_helper`.")),
        _beats(("s1", "The delay doubles.")),
    )
    result = plan_beats(doc, index, _metered(provider))
    assert result.ok
    assert any("unrelated_helper" in r for r in result.rejections)


def test_beats_may_be_reordered_across_sections(index, repo):
    """Document order is for readers, who can scroll."""
    doc = _doc(index, _section(index, id="s1"), _section(index, id="s2"))
    provider = ScriptedProvider(_beats(("s2", "Second first."), ("s1", "First second.")))
    result = plan_beats(doc, index, _metered(provider))
    assert result.plan.sections == ("s2", "s1")


# -- the rule that must stay positive ---------------------------------------


def test_a_speculative_beat_must_be_hedged_out_loud(index, repo):
    """A listener cannot see a `speculative` tag. If the words do not sound
    uncertain, the audio asserts something the document hedged.

    Stated positively — 'must contain a hedge' — this is checkable. Stated
    negatively — 'must not sound overconfident' — it is not, and a model told
    to avoid sounding a way will simply stop sounding that way.
    """
    plan = _plan(_beat(confidence="speculative"))
    provider = ScriptedProvider(
        _lines((0, "The delay doubles on every attempt.")),
        _lines((0, "The delay probably doubles on every attempt.")),
    )
    result = write_narration(plan, index, _metered(provider))
    assert result.ok
    assert result.attempts == 2
    assert any("speculative" in r for r in result.rejections)
    assert has_hedge(result.narration.segments[-1].text)


def test_a_confident_beat_needs_no_hedge(index, repo):
    plan = _plan(_beat(confidence="verified"))
    provider = ScriptedProvider(_lines((0, "The delay doubles on every attempt.")))
    result = write_narration(plan, index, _metered(provider))
    assert result.ok and result.attempts == 1


def test_the_hedge_check_is_lexical_not_a_judgement():
    assert has_hedge("This probably retries.") is True
    assert has_hedge("It appears to retry.") is True
    assert has_hedge("It retries.") is False
    # A hedge is a hedge wherever it lands in the sentence.
    assert has_hedge("Retries are capped, seems to be at sixty.") is True


# -- the signpost is computed, not requested --------------------------------


def test_a_background_segment_is_signposted_without_being_asked(index, repo):
    """Prepending makes it structural. Asking for it makes it omittable, and a
    model forgets it on beat nineteen."""
    plan = _plan(_beat(kind="background", point="Most libraries add jitter."))
    provider = ScriptedProvider(_lines((0, "Most libraries add jitter.")))
    result = write_narration(plan, index, _metered(provider))
    assert result.ok
    assert result.attempts == 1  # never rejected for omitting it
    assert result.narration.segments[-1].text.startswith(BACKGROUND_PREFIX)


def test_a_repository_segment_gets_no_signpost(index, repo):
    plan = _plan(_beat(kind="structure"))
    provider = ScriptedProvider(_lines((0, "The delay doubles.")))
    result = write_narration(plan, index, _metered(provider))
    assert BACKGROUND_PREFIX not in result.narration.segments[-1].text


# -- segments are scenes ----------------------------------------------------


def test_a_segment_longer_than_a_scene_is_rejected(index, repo):
    """One segment is one scene once the video renderer exists, and it is also
    the TTS cache unit. Four sentences is a scene that outstays its welcome and
    a cache entry that reruns on every edit."""
    long = " ".join(f"Sentence number {n}." for n in range(1, MAX_SENTENCES + 3))
    provider = ScriptedProvider(_lines((0, long)), _lines((0, "The delay doubles.")))
    result = write_narration(_plan(_beat()), index, _metered(provider))
    assert result.ok
    assert any("sentence" in r for r in result.rejections)


def test_markup_is_stripped_before_it_reaches_a_voice():
    """A backtick read aloud is either pronounced or stumbled over, and the
    source documents are full of them."""
    assert speakable("It calls `backoff_seconds` twice.") == "It calls backoff_seconds twice."
    assert speakable("It is **fast**.") == "It is fast."


def test_the_narration_opens_by_saying_what_it_is(index, repo):
    """A reader chose to open a page and can see its title. A listener may have
    joined a file with no context at all."""
    provider = ScriptedProvider(_lines((0, "The delay doubles.")))
    result = write_narration(_plan(_beat()), index, _metered(provider))
    first = result.narration.segments[0]
    assert first.role == "opening"
    assert "T" in first.text and "Q" in first.text


def test_the_caveats_are_spoken_not_only_shown(index, repo):
    """On the page they are a box the reader sees before starting. In audio,
    saying nothing about them is a claim that the run saw everything."""
    doc = _doc(index, _section(index), git="shallow", dirty=True)
    provider = ScriptedProvider(_lines((0, "The delay doubles.")))
    result = write_narration(_plan(_beat()), index, _metered(provider), document=doc)
    last = result.narration.segments[-1]
    assert last.role == "closing"
    assert "caveat" in last.text.lower()


def test_a_clean_run_gets_no_closing_caveat(index, repo):
    # `forge="none"` is itself a caveat and is the default, so a document with
    # nothing to disclose has to say so explicitly.
    doc = _doc(index, _section(index), forge="authenticated")
    provider = ScriptedProvider(_lines((0, "The delay doubles.")))
    result = write_narration(_plan(_beat()), index, _metered(provider), document=doc)
    assert all(s.role != "closing" for s in result.narration.segments)


# -- the voice port ---------------------------------------------------------


def test_the_cache_key_changes_when_the_voice_changes(tmp_path):
    """The failure this prevents has no symptom: upgrade piper, edit one
    paragraph, and one segment returns in a different timbre while every
    artifact hash reports a clean run."""
    segment = Segment(
        id="n001", section_id="s1", beat_order=0, text="Same words.",
        confidence="derived", kind="structure",
    )
    quick = SilenceEngine(words_per_minute=150)
    slow = SilenceEngine(words_per_minute=90)
    assert segment_key(segment, quick) != segment_key(segment, slow)
    assert segment_key(segment, quick) == segment_key(segment, SilenceEngine(150))


def test_a_missing_piper_names_both_halves_of_what_is_missing(tmp_path):
    """Telling someone who installed piper that piper is missing sends them to
    re-check the half that is already fine."""
    engine = PiperEngine(binary="definitely-not-a-real-binary", voice=None)
    state = engine.check()
    assert not state
    assert "definitely-not-a-real-binary" in state.reason
    assert "voice" in state.reason


def test_piper_degrades_with_a_reason_instead_of_raising(tmp_path):
    engine = PiperEngine(binary="definitely-not-a-real-binary")
    spoken = engine.speak("Hello.", tmp_path / "out.wav")
    assert isinstance(spoken, Spoken)
    assert spoken.path is None and spoken.reason


def test_a_missing_voice_model_is_distinguishable_from_a_missing_binary(tmp_path):
    engine = PiperEngine(binary="sh", voice=tmp_path / "nope.onnx")
    reason = engine.check().reason
    assert "does not exist" in reason
    assert "PATH" not in reason


# -- the audio chain --------------------------------------------------------


@has_ffmpeg
def test_duration_is_measured_not_estimated(tmp_path):
    engine = SilenceEngine()
    out = tmp_path / "seg.wav"
    engine.speak("one two three four five six seven eight", out)
    measured = duration_of(out)
    assert measured is not None
    assert 0.5 < measured < 10


@has_ffmpeg
def test_the_timeline_is_contiguous_and_ordered(tmp_path):
    narration = Narration(
        title="T", question="Q", base_sha="abc",
        segments=tuple(
            Segment(
                id=f"n{n:03d}", section_id="s1", beat_order=n,
                text=f"Segment number {n} says something short.",
                confidence="derived", kind="structure",
            )
            for n in range(3)
        ),
    )
    result = build_audio(
        narration, SilenceEngine(), out=tmp_path / "a.wav", cache_dir=tmp_path / "c"
    )
    assert result.ok
    assert [t.id for t in result.timings] == ["n000", "n001", "n002"]
    for earlier, later in zip(result.timings, result.timings[1:]):
        assert later.start == pytest.approx(earlier.end, abs=0.002)
    assert result.estimated is False


@has_ffmpeg
def test_the_joined_audio_is_as_long_as_its_parts(tmp_path):
    """The concat step is where a silently dropped segment would hide."""
    narration = Narration(
        title="T", question="", base_sha="abc",
        segments=tuple(
            Segment(
                id=f"n{n:03d}", section_id="s1", beat_order=n,
                text="Four words go here.", confidence="derived", kind="structure",
            )
            for n in range(4)
        ),
    )
    result = build_audio(
        narration, SilenceEngine(), out=tmp_path / "a.wav", cache_dir=tmp_path / "c"
    )
    assert duration_of(result.path) == pytest.approx(result.seconds, abs=0.05)


@has_ffmpeg
def test_a_second_run_synthesizes_nothing(tmp_path):
    narration = Narration(
        title="T", question="", base_sha="abc",
        segments=(
            Segment(id="n000", section_id="s1", beat_order=0, text="A line.",
                    confidence="derived", kind="structure"),
        ),
    )
    cache = tmp_path / "c"
    first = build_audio(narration, SilenceEngine(), out=tmp_path / "a.wav", cache_dir=cache)
    second = build_audio(narration, SilenceEngine(), out=tmp_path / "b.wav", cache_dir=cache)
    assert first.synthesized == 1 and first.reused == 0
    assert second.synthesized == 0 and second.reused == 1


def test_no_engine_still_produces_a_timeline_that_admits_it(tmp_path):
    """Partial-that-says-so: the video renderer can lay out scenes against an
    estimate, but nothing may mistake it for a measurement."""
    narration = Narration(
        title="T", question="", base_sha="abc",
        segments=(
            Segment(id="n000", section_id="s1", beat_order=0, text="A line of words here.",
                    confidence="derived", kind="structure"),
        ),
    )
    engine = PiperEngine(binary="definitely-not-a-real-binary")
    result = build_audio(
        narration, engine, out=tmp_path / "a.wav", cache_dir=tmp_path / "c"
    )
    assert result.path is None
    assert result.reason
    assert result.timings and result.estimated is True
    assert json.loads(timings_json(result))["estimated"] is True


@has_ffmpeg
def test_the_timings_artifact_carries_the_engine_identity(tmp_path):
    """So a timeline can never be read back without knowing which voice made it."""
    narration = Narration(
        title="T", question="", base_sha="abc",
        segments=(
            Segment(id="n000", section_id="s1", beat_order=0, text="A line.",
                    confidence="derived", kind="structure"),
        ),
    )
    result = build_audio(
        narration, SilenceEngine(), out=tmp_path / "a.wav", cache_dir=tmp_path / "c"
    )
    payload = json.loads(timings_json(result))
    assert payload["engine"].startswith("silence/")
    assert payload["segments"][0]["end"] > payload["segments"][0]["start"]


@has_ffmpeg
def test_a_path_with_a_quote_in_it_survives_concatenation(tmp_path):
    """The cache directory is user-supplied, and `/home/o'brien/` is not exotic."""
    cache = tmp_path / "o'brien"
    narration = Narration(
        title="T", question="", base_sha="abc",
        segments=(
            Segment(id="n000", section_id="s1", beat_order=0, text="A line.",
                    confidence="derived", kind="structure"),
        ),
    )
    result = build_audio(narration, SilenceEngine(), out=tmp_path / "a.wav", cache_dir=cache)
    assert result.ok, result.reason


@has_ffmpeg
def test_the_concat_list_does_not_survive_the_run(tmp_path):
    narration = Narration(
        title="T", question="", base_sha="abc",
        segments=(
            Segment(id="n000", section_id="s1", beat_order=0, text="A line.",
                    confidence="derived", kind="structure"),
        ),
    )
    build_audio(narration, SilenceEngine(), out=tmp_path / "a.wav", cache_dir=tmp_path / "c")
    assert not list(tmp_path.glob(".*concat.txt"))


# -- the command ------------------------------------------------------------


@has_ffmpeg
def test_the_command_writes_every_artifact_separately(index, repo, tmp_path, monkeypatch):
    """Beats, script, timings and audio are four files because they have four
    different costs. Re-voicing should never mean re-writing."""
    doc = _doc(index, _section(index))
    source = tmp_path / "doc.json"
    doc.write(source)

    out = tmp_path / "audio"
    out.mkdir()
    _seed(out, doc)
    from kreb.cli import main

    code = main(
        ["--repo", str(repo.root), "--json", "audio", str(source),
         "--out", str(out), "--voice", "silence"]
    )
    assert code == 0
    assert (out / "script.txt").exists()
    assert (out / "timings.json").exists()
    assert (out / "narration.wav").exists()


def test_the_command_keeps_the_writing_when_there_is_no_voice(
    index, repo, tmp_path, capsys
):
    """A missing voice model must not cost you the script you already paid for."""
    doc = _doc(index, _section(index))
    source = tmp_path / "doc.json"
    doc.write(source)
    out = tmp_path / "audio"
    out.mkdir()
    _seed(out, doc)
    from kreb.cli import main

    code = main(
        ["--repo", str(repo.root), "--json", "audio", str(source),
         "--out", str(out), "--voice", str(tmp_path / "missing.onnx")]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1  # scriptable: no audio is a mechanical failure
    assert payload["audio"] is None
    assert "does not exist" in payload["reason"]
    assert payload["estimated"] is True
    assert (out / "script.txt").read_text().strip() == "The delay doubles."
    assert json.loads((out / "timings.json").read_text())["segments"]


def test_a_spoken_caveat_drops_what_only_works_on_a_page(index, repo):
    """`14 files (14/64) had no symbol index` is a useful glance on the page and
    an unreadable mouthful out loud. Found by listening to the first real run."""
    doc = _doc(index, _section(index), degraded_files=14, total_files=64)
    provider = ScriptedProvider(_lines((0, "The delay doubles.")))
    result = write_narration(_plan(_beat()), index, _metered(provider), document=doc)
    spoken = result.narration.segments[-1].text
    assert "(14/64)" not in spoken
    assert "14 files had no symbol index" in spoken


def test_a_ratio_outside_parentheses_is_spoken_as_words(index, repo):
    from kreb.render.narration import _for_the_ear

    assert _for_the_ear("3/4 of files were skipped.") == "3 of 4 of files were skipped."


# -- reuse must be keyed on identity, not existence -------------------------


def test_a_plan_knows_which_document_it_came_from(index, repo):
    doc_a = _doc(index, _section(index, body="First."))
    doc_b = _doc(index, _section(index, body="Second."))
    provider = ScriptedProvider(_beats(("s1", "The delay doubles.")))
    plan = plan_beats(doc_a, index, _metered(provider)).plan
    assert plan.matches(doc_a) is True
    assert plan.matches(doc_b) is False


def test_a_plan_without_a_digest_is_treated_as_a_mismatch(index, repo):
    """Regenerating costs a few cents; narrating the wrong document costs the
    whole artifact."""
    doc = _doc(index, _section(index))
    assert _plan(_beat()).matches(doc) is False


@has_ffmpeg
def test_narrating_a_second_document_does_not_replay_the_first(
    index, repo, tmp_path, capsys
):
    """`--out` defaults to one fixed path, so reuse keyed on file existence
    means the second document silently gets the first one's beats."""
    from kreb.cli import main

    doc_a = _doc(index, _section(index, body="First document."))
    doc_b = _doc(index, _section(index, body="A different document entirely."))
    out = tmp_path / "audio"
    out.mkdir()

    stale = _plan(_beat(point="From the first document."))
    stale.doc_digest = "0" * 16
    (out / "beats.json").write_text(beats_to_json(stale))
    (out / "narration.json").write_text(
        json.dumps(
            {
                "title": "T", "question": "", "base_sha": "abc",
                "segments": [
                    {"id": "n000", "section_id": "s1", "beat_order": 0,
                     "text": "From the first document.", "confidence": "derived",
                     "kind": "structure", "role": "beat"}
                ],
            }
        )
    )
    source = tmp_path / "b.json"
    doc_b.write(source)

    # No API key in the test environment, so the rewrite path bails at the key
    # check — which is itself the proof it refused to reuse.
    code = main(
        ["--repo", str(repo.root), "audio", str(source),
         "--out", str(out), "--voice", "silence"]
    )
    err = capsys.readouterr().err
    assert "different document" in err
    assert code != 0
    assert "From the first document." not in (out / "script.txt").read_text(
    ) if (out / "script.txt").exists() else True


# -- a dropped segment is not a success -------------------------------------


@has_ffmpeg
def test_a_dropped_segment_makes_the_run_incomplete(tmp_path):
    """`beats` enforces that every section gets a beat; a segment lost at
    synthesis undoes that one layer down, and the file still plays."""

    class HalfBrokenEngine(SilenceEngine):
        def speak(self, text, out):
            if "second" in text:
                return Spoken(path=None, reason="synthetic failure")
            return super().speak(text, out)

    narration = Narration(
        title="T", question="", base_sha="abc",
        segments=(
            Segment(id="n000", section_id="s1", beat_order=0, text="The first line.",
                    confidence="derived", kind="structure"),
            Segment(id="n001", section_id="s2", beat_order=1, text="The second line.",
                    confidence="derived", kind="structure"),
        ),
    )
    result = build_audio(
        narration, HalfBrokenEngine(), out=tmp_path / "a.wav", cache_dir=tmp_path / "c"
    )
    assert result.ok is True       # the file exists and plays
    assert result.complete is False  # and is missing a section
    assert "1 of 2 segments" in result.reason
    payload = json.loads(timings_json(result))
    assert payload["complete"] is False
    assert payload["failures"]


# -- the hedge check must not be defeatable by spelling ---------------------


def test_a_word_merely_containing_a_hedge_is_not_a_hedge():
    """"The mighty parser" contains "might". Substring matching would pass it
    as hedged, which is the rule quietly ceasing to be a rule."""
    assert has_hedge("The mighty parser handles this.") is False
    assert has_hedge("Dismay is not a hedge.") is False
    assert has_hedge("It might be the parser.") is True


def test_a_hedge_at_the_end_of_a_sentence_still_counts():
    assert has_hedge("The parser handles it, or so it may.") is True
    assert has_hedge("Retries are capped, it seems.") is True
