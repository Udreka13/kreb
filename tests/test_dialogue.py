"""The two-host renderer and the cast that voices it.

The load-bearing test is `test_a_host_may_not_name_a_symbol_the_section_never_cited`.
A second speaker is a second mouth that can name things that do not exist, and
the symbol allowlist is set containment against the index — mechanically true or
false, which is why it belongs in the retry loop.

What is deliberately *not* tested here is how the script sounds. An earlier
version made the host end every turn in a question mark and capped it at one
sentence; those rules produced the stilted output they looked like they
prevented. Naturalness comes from the prompt and is judged in
`tests/test_critique.py`, where the verdict is a report rather than a gate.
"""

from __future__ import annotations

import json
import subprocess
from argparse import Namespace

import pytest

from kreb.doc.schema import Anchor, Capabilities, Document, Section
from kreb.index.repo_index import build_index
from kreb.media import check_tools
from kreb.render.audio import build_audio, segment_key
from kreb.render.beats import Beat, BeatsPlan
from kreb.render.dialogue import EXPERT, HOST, transcript, write_dialogue
from kreb.render.narration import MAX_SENTENCES
from kreb.render.narration import BACKGROUND_PREFIX, from_json, to_json
from kreb.repo.access import Repository
from kreb.tts.cast import Cast, as_cast
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


def _section(index, *, id="s1", confidence="derived", kind="structure"):
    symbol = index.symbols["retry.py#backoff_seconds"]
    return Section(
        id=id,
        title="Backoff",
        kind=kind,
        body="Prose.",
        confidence=confidence,
        anchors=(Anchor(ref=symbol.ref, text_hash=symbol.text_hash, lines=(4, 5)),),
    )


def _doc(index, *sections, **caps):
    return Document(
        title="How retries work",
        question="How does backoff work?",
        capabilities=Capabilities(base_sha=index.sha, forge="authenticated", **caps),
        sections=tuple(sections),
    )


def _beat(order=0, *, confidence="derived", kind="structure"):
    return Beat(
        section_id="s1",
        section_title="Backoff",
        key_point="The delay doubles.",
        confidence=confidence,
        kind=kind,
        order=order,
    )


def _plan(*beats):
    return BeatsPlan(title="T", question="How does backoff work?", base_sha="abc",
                     beats=tuple(beats or (_beat(),)))


def _turns(*items):
    """(order, host, expert) triples as the model would return them."""
    return json.dumps(
        {"turns": [{"order": o, "host": h, "expert": e} for o, h, e in items]}
    )


def _body(narration):
    """The conversation without the computed opening and closing."""
    return [s for s in narration.segments if s.role in ("beat", "question")]


def _run(index, plan, *payloads, document=None):
    return write_dialogue(
        plan, index, _metered(ScriptedProvider(*payloads)), document=document
    )


# -- the host is held to the same symbols as the expert ----------------------


def test_a_host_may_not_name_a_symbol_the_section_never_cited(index, repo):
    """The one rule the host still has. A question naming a symbol is still a
    claim that the symbol exists and is relevant here — a fabricated anchor
    with a question mark after it."""
    doc = _doc(index, _section(index))
    result = _run(
        index,
        _plan(),
        _turns((0, "Does unrelated_helper come into this?", "The delay doubles.")),
        _turns((0, "Does that hold under load?", "The delay doubles.")),
        document=doc,
    )
    assert result.ok
    assert any("unrelated_helper" in r for r in result.rejections)


def test_a_host_turn_need_not_be_a_question(index, repo):
    """Statements from the host are legal now. "So that's why the hashes are
    split." is a real thing a person says, and a validator that rejected it was
    enforcing a style nobody asked for."""
    result = _run(
        index, _plan(), _turns((0, "So that's the whole trick.", "The delay doubles."))
    )
    assert result.ok
    assert result.attempts == 1
    assert _body(result.narration)[0].text == "So that's the whole trick."


def test_a_host_may_take_more_than_one_sentence(index, repo):
    """A person reacting before they ask is what a conversation sounds like."""
    result = _run(
        index,
        _plan(),
        _turns((0, "Huh. So what happens on a retry?", "The delay doubles.")),
    )
    assert result.ok
    assert result.attempts == 1


def test_a_script_where_nobody_asks_anything_is_accepted(index, repo):
    """Judged, not rejected. Whether a script is a conversation or a monologue
    with labels is a semantic question, and this loop only rejects on
    mechanical ones."""
    result = _run(
        index,
        _plan(_beat(0), _beat(1)),
        _turns((0, "", "The delay doubles."), (1, "", "It is capped.")),
    )
    assert result.ok
    assert result.attempts == 1


def test_a_beat_may_skip_its_question(index, repo):
    """Silence from the host is not a failure. A question on every beat is a
    metronome, and the prompt asks for restraint rather than the validator
    demanding participation."""
    result = _run(
        index,
        _plan(_beat(0), _beat(1)),
        _turns((0, "What happens on a retry?", "The delay doubles."), (1, "", "It is capped.")),
    )
    assert result.ok
    assert result.attempts == 1
    speakers = [s.speaker for s in _body(result.narration)]
    assert speakers.count(HOST) == 1
    assert speakers.count(EXPERT) == 2


def test_a_long_expert_turn_is_split_not_rejected(index, repo):
    """A stretch where the expert just runs with it is one of the things that
    makes audio sound like a person. The scene cap is a video constraint, so it
    cuts the turn rather than refusing the writing."""
    long_answer = (
        "The delay doubles. It caps at sixty seconds. That keeps a retry storm "
        "bounded. And the cap is a constant, not a setting."
    )
    result = _run(index, _plan(), _turns((0, "How does the backoff work?", long_answer)))
    assert result.ok
    assert result.attempts == 1
    expert = [s for s in _body(result.narration) if s.speaker == EXPERT]
    assert len(expert) == 2
    assert all(len(s.sentences) <= MAX_SENTENCES for s in expert)
    # Nothing the model wrote is lost in the cut.
    assert " ".join(s.text for s in expert) == long_answer


def test_a_host_turn_is_never_split(index, repo):
    """"Wait, hang on. Not even a little?" is three sentences and one breath.
    Cutting it puts a scene boundary inside a single thought — the cap is for
    the expert's long stretches, and here it only costs the delivery."""
    breath = "Wait, hang on. Nothing? Not even a little?"
    result = _run(index, _plan(), _turns((0, breath, "Nothing at all.")))
    assert result.ok
    hosts = [s for s in _body(result.narration) if s.speaker == HOST]
    assert len(hosts) == 1
    assert hosts[0].text == breath


def test_a_split_turn_keeps_its_beat_and_confidence(index, repo):
    """Each piece is still that beat's claim, so it inherits the same tags — a
    cut that dropped them would launder a speculative line into a flat one."""
    answer = "It probably doubles. It may cap out. Something like sixty seconds."
    result = _run(index, _plan(_beat(confidence="speculative")),
                  _turns((0, "", answer)))
    assert result.ok
    expert = [s for s in _body(result.narration) if s.speaker == EXPERT]
    assert len(expert) == 2
    assert all(s.confidence == "speculative" for s in expert)
    assert all(s.beat_order == 0 for s in expert)


def test_split_pieces_get_distinct_ids(index, repo):
    """The id is the TTS cache key. Two pieces sharing one would collapse into a
    single cached file and drop half the answer."""
    answer = "One. Two. Three. Four."
    result = _run(index, _plan(), _turns((0, "", answer)))
    assert result.ok
    ids = [s.id for s in _body(result.narration)]
    assert len(ids) == len(set(ids))


def test_hesitation_and_repetition_survive(index, repo):
    """The point of the whole change. None of this reads well and all of it is
    what a person actually sounds like."""
    messy = "Right, right. So — okay, so the delay doubles, doubles each time."
    result = _run(index, _plan(), _turns((0, "Wait, hang on. Say that again?", messy)))
    assert result.ok
    assert result.attempts == 1
    assert any(s.text == messy for s in _body(result.narration))


# -- the expert keeps every rule the narrator had ----------------------------


def test_a_speculative_beat_must_still_be_hedged_out_loud(index, repo):
    """The rule survives the second renderer. A hedge that only applies to the
    monologue is a hedge that disappears the moment you pick the other style."""
    result = _run(
        index,
        _plan(_beat(confidence="speculative")),
        _turns((0, "What happens on a retry?", "The delay doubles.")),
        _turns((0, "What happens on a retry?", "The delay probably doubles.")),
    )
    assert result.ok
    assert result.attempts == 2
    assert any("speculative" in r for r in result.rejections)


def test_the_hedge_belongs_to_the_answer_not_the_question(index, repo):
    """A question containing "probably" is not the expert expressing doubt, and
    accepting it there would let every hedge be satisfied by the wrong mouth."""
    result = _run(
        index,
        _plan(_beat(confidence="speculative")),
        _turns((0, "Does it probably double?", "The delay doubles.")),
        _turns((0, "Does it double?", "The delay probably doubles.")),
    )
    assert result.ok
    assert result.attempts == 2


def test_the_background_signpost_lands_on_the_expert(index, repo):
    """The signpost marks a claim as coming from outside the codebase, and the
    expert is who makes claims."""
    result = _run(
        index,
        _plan(_beat(kind="background")),
        _turns((0, "Why does anyone do it this way?", "Exponential backoff is standard.")),
    )
    assert result.ok
    body = _body(result.narration)
    assert body[0].speaker == HOST
    assert not body[0].text.startswith(BACKGROUND_PREFIX)
    assert body[1].text.startswith(BACKGROUND_PREFIX)


def test_a_beat_with_no_expert_answer_is_rejected(index, repo):
    """Coverage is the invariant `beats` exists to enforce, and it has to
    survive one layer down or a section vanishes from the audio."""
    result = _run(
        index,
        _plan(_beat(0), _beat(1)),
        _turns((0, "What happens on a retry?", "The delay doubles."), (1, "And then?", "")),
        _turns((0, "What happens on a retry?", "The delay doubles."), (1, "And then?", "Capped.")),
    )
    assert result.ok
    assert any("no answer" in r for r in result.rejections)


# -- shape ------------------------------------------------------------------


def test_the_question_is_asked_before_it_is_answered(index, repo):
    result = _run(index, _plan(), _turns((0, "What happens on a retry?", "The delay doubles.")))
    assert result.ok
    beats = _body(result.narration)
    assert [s.speaker for s in beats] == [HOST, EXPERT]


def test_the_opening_asks_the_documents_own_question(index, repo):
    """A listener who joins an audio file has none of the context a reader gets
    from a page they chose to open."""
    result = _run(index, _plan(), _turns((0, "And after that?", "The delay doubles.")))
    assert result.ok
    opening = [s for s in result.narration.segments if s.role == "opening"]
    assert len(opening) == 1
    assert opening[0].speaker == HOST
    assert opening[0].text == "How does backoff work?"


def test_nothing_computed_answers_the_opening_question(index, repo):
    """Found by the judge on the first real run.

    An expert reply here was a disclaimer rather than an answer, so the model —
    handed the same question as beat zero — asked it again, and the script
    opened by asking one question twice with a non-answer in between. The
    expert's first words have to be the model's, answering for real.
    """
    result = _run(index, _plan(), _turns((0, "So what is it?", "It doubles the delay.")))
    assert result.ok
    segments = result.narration.segments
    assert segments[0].speaker == HOST
    assert segments[1].speaker == HOST  # beat zero's question, not a computed reply
    assert segments[2].text == "It doubles the delay."


def test_the_caveats_are_asked_for_and_then_spoken(index, repo):
    doc = _doc(index, _section(index), git="shallow")
    result = _run(
        index, _plan(), _turns((0, "What happens on a retry?", "The delay doubles.")),
        document=doc,
    )
    assert result.ok
    closing = [s for s in result.narration.segments if s.role == "closing"]
    assert [s.speaker for s in closing] == [HOST, EXPERT]
    assert closing[0].text.endswith("?")
    # Spoken phrasing, not the page's. "Shallow clone: no history, so no
    # rationale could be recovered." is a log line, and no punctuation fix turns
    # a log line into a sentence someone says out loud.
    assert "shallow clone" in closing[1].text.lower()
    assert ":" not in closing[1].text


def test_a_clean_run_gets_no_closing_exchange(index, repo):
    doc = _doc(index, _section(index))
    result = _run(
        index, _plan(), _turns((0, "What happens on a retry?", "The delay doubles.")),
        document=doc,
    )
    assert result.ok
    assert [s for s in result.narration.segments if s.role == "closing"] == []


def test_the_transcript_names_who_is_speaking(index, repo):
    result = _run(index, _plan(), _turns((0, "What happens on a retry?", "The delay doubles.")))
    text = transcript(result.narration)
    assert "HOST:" in text and "EXPERT:" in text


def test_the_speaker_survives_a_round_trip(index, repo):
    """`speaker` selects the voice, so losing it in serialization means the
    second run reads the whole conversation in one timbre."""
    result = _run(index, _plan(), _turns((0, "What happens on a retry?", "The delay doubles.")))
    back = from_json(to_json(result.narration))
    assert [s.speaker for s in back.segments] == [s.speaker for s in result.narration.segments]


# -- what the prompt asks for -----------------------------------------------


def test_the_prompt_forbids_inventing_facts_and_inventing_a_world():
    """The two things a model must not supply. Facts come from the beats; the
    room is real or it is not mentioned. A fabricated anecdote about the weather
    is a fabrication however charming, and it is the one kind this pipeline
    cannot catch downstream — no anchor, no symbol, nothing to check it against."""
    from kreb.render.dialogue import DIALOGUE_SYSTEM

    assert "come from the beats below and nowhere else" in DIALOGUE_SYSTEM
    assert "weather" in DIALOGUE_SYSTEM
    assert "fabrication" in DIALOGUE_SYSTEM


def test_the_prompt_does_not_police_style():
    """Guidance shrank on purpose. Filler keeps a conversation moving out loud
    even though it reads badly, so instructions against it were removed — and a
    prompt that grows a taste rule back is the regression."""
    from kreb.render.dialogue import DIALOGUE_SYSTEM

    for banned in ("filler", "Great question", "metronome", "at most", "one sentence"):
        assert banned not in DIALOGUE_SYSTEM


def test_the_prompt_asks_for_real_speech():
    from kreb.render.dialogue import DIALOGUE_SYSTEM

    assert "hesitate" in DIALOGUE_SYSTEM
    assert "uneven" in DIALOGUE_SYSTEM


def test_the_prompt_warns_against_a_host_with_one_move():
    """The judge's finding on the first real run: five host turns in a row built
    as "And the X —". Casual words in an identical frame still read as a form
    being filled in, and nothing mechanical can catch it — so it is the prompt's
    job, and the prompt losing it is the regression."""
    from kreb.render.dialogue import DIALOGUE_SYSTEM

    assert "Watch the host in particular" in DIALOGUE_SYSTEM
    assert "form being filled in" in DIALOGUE_SYSTEM


# -- the cast ---------------------------------------------------------------


def test_a_lone_engine_is_a_cast_of_one():
    engine = SilenceEngine()
    cast = as_cast(engine)
    assert cast.for_speaker("host") is engine
    assert cast.identity == engine.identity


def test_a_cast_routes_each_speaker_to_its_own_voice():
    host, expert = SilenceEngine(sample_rate=22050), SilenceEngine(words_per_minute=180)
    cast = Cast(default=expert, voices={"host": host, "expert": expert})
    assert cast.for_speaker("host") is host
    assert cast.for_speaker("expert") is expert


def test_an_unknown_speaker_falls_back_rather_than_failing():
    """A dialogue played through a one-voice cast should be audible in one
    voice. A run of failed segments would be a worse answer to a wrong-sounding
    result that is obvious on the first listen."""
    default = SilenceEngine()
    cast = Cast(default=default, voices={"host": SilenceEngine(words_per_minute=180)})
    assert cast.for_speaker("narrator") is default


def test_recasting_invalidates_the_cache():
    """Swapping which voice plays the host must not serve back the old host's
    audio — the same seam the engine identity exists to prevent, one level up."""
    from kreb.render.narration import Segment

    segment = Segment(id="s1", section_id="sec", beat_order=0, text="A line.",
                      confidence="verified", kind="structure", speaker="host")
    a = Cast(default=SilenceEngine(), voices={"host": SilenceEngine(words_per_minute=180)})
    b = Cast(default=SilenceEngine(), voices={"host": SilenceEngine(words_per_minute=200)})
    assert segment_key(segment, a.for_speaker("host")) != segment_key(
        segment, b.for_speaker("host")
    )


def test_a_cast_names_every_missing_voice_at_once():
    """Failing on the first would send someone to fix their expert voice, rerun,
    and be told about the host."""
    from kreb.tts.piper import PiperEngine

    cast = Cast(
        default=PiperEngine(binary="definitely-not-real", voice=None),
        voices={"host": PiperEngine(binary="also-not-real", voice=None)},
    )
    state = cast.check()
    assert not state
    assert "narrator" in state.reason and "host" in state.reason


def test_the_cast_identity_changes_with_its_voices():
    one = Cast(default=SilenceEngine(), voices={"host": SilenceEngine(words_per_minute=180)})
    two = Cast(default=SilenceEngine(), voices={"host": SilenceEngine(words_per_minute=200)})
    assert one.identity != two.identity


@has_ffmpeg
def test_two_voices_produce_two_cache_entries(tmp_path, index, repo):
    """End to end: the host and expert lines land in different files even when
    the text is identical, because the voice is part of the key."""
    from kreb.render.narration import Narration, Segment

    narration = Narration(
        title="T", question="", base_sha="abc",
        segments=(
            Segment(id="q", section_id="s1", beat_order=0, text="Same words.",
                    confidence="verified", kind="structure", speaker="host"),
            Segment(id="a", section_id="s1", beat_order=0, text="Same words.",
                    confidence="verified", kind="structure", speaker="expert"),
        ),
    )
    cast = Cast(
        default=SilenceEngine(),
        voices={"host": SilenceEngine(words_per_minute=90), "expert": SilenceEngine()},
    )
    cache = tmp_path / "c"
    result = build_audio(narration, cast, out=tmp_path / "a.wav", cache_dir=cache)
    assert result.ok, result.reason
    assert result.synthesized == 2
    assert len(list(cache.glob("*.wav"))) == 2
    # Different rates, so the two lines are genuinely different audio.
    assert result.timings[0].seconds != result.timings[1].seconds


# -- CLI --------------------------------------------------------------------


def _args(**extra) -> Namespace:
    base = dict(
        voice="silence", voice_name="", speed=1.0,
        style="dialogue", host_voice="", host_voice_name="",
    )
    base.update(extra)
    return Namespace(**base)


def test_dialogue_with_a_hosted_voice_gets_two_timbres_from_one_flag():
    from kreb.cli import engine_for
    from kreb.tts.openrouter import OpenRouterVoice

    cast = engine_for(
        _args(voice="deepgram/flux-tts:free", voice_name="alloy", host_voice_name="nova")
    )
    assert isinstance(cast, Cast)
    assert isinstance(cast.for_speaker("host"), OpenRouterVoice)
    assert cast.for_speaker("host").voice == "nova"
    assert cast.for_speaker("expert").voice == "alloy"


def test_a_separate_host_engine_is_accepted():
    from kreb.cli import engine_for
    from kreb.tts.silence import SilenceEngine as S

    cast = engine_for(_args(voice="silence", host_voice="deepgram/flux-tts:free"))
    assert isinstance(cast.for_speaker("expert"), S)
    assert cast.for_speaker("host").model == "deepgram/flux-tts:free"


def test_a_dialogue_that_cannot_be_split_says_so(capsys):
    """Silence has no timbre and piper needs a second model file, so neither can
    be re-voiced by name. Collapsing quietly would leave someone reading a
    two-speaker transcript and hearing one voice with no explanation."""
    from kreb.cli import engine_for

    engine = engine_for(_args(voice="silence"))
    assert not isinstance(engine, Cast)
    assert "one voice reads both parts" in capsys.readouterr().err


def test_monologue_gets_a_plain_engine_not_a_cast():
    from kreb.cli import engine_for

    assert not isinstance(engine_for(_args(style="monologue")), Cast)


def test_the_command_defaults_to_dialogue():
    """The format the listener actually gets. A default of monologue would make
    the two-host renderer something you have to know exists."""
    from kreb.cli import build_parser

    assert build_parser().parse_args(["audio", "d.json"]).style == "dialogue"
