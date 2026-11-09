import asyncio
import csv
import json

from avenza_ai.providers import MockTTSProvider
from avenza_ai.providers.audio import pcm_to_wav_bytes, tone_pcm, wav_bytes_to_pcm
from avenza_ai.speech.prompt_library import build_library, export_review_sheet, load_manifest, verify_library
from avenza_ai.speech.prompts import PROMPT_IDS, load_prompt_file

run = asyncio.run


def test_build_and_verify(tmp_path):
    tts = MockTTSProvider()
    m = run(build_library(tts, tmp_path, ["en", "pcm"]))
    assert len(m) == 2 * len(PROMPT_IDS) and verify_library(tmp_path, ["en", "pcm"]) == []
    assert verify_library(tmp_path, ["yo"]) == [f"yo/{i}" for i in PROMPT_IDS]
    pcm, rate = wav_bytes_to_pcm((tmp_path / "en" / "cancelled.wav").read_bytes())
    assert pcm and rate == 24000


def test_second_run_uses_cache(tmp_path):
    tts = MockTTSProvider()
    run(build_library(tts, tmp_path, ["en"]))
    n = len(tts.calls)
    run(build_library(tts, tmp_path, ["en"]))
    assert len(tts.calls) == n
    run(build_library(tts, tmp_path, ["en"], force=True))
    assert len(tts.calls) == 2 * n


def test_changed_voice_regenerates(tmp_path):
    tts = MockTTSProvider()
    run(build_library(tts, tmp_path, ["en"], voice_by_language={"en": "A"}))
    n = len(tts.calls)
    run(build_library(tts, tmp_path, ["en"], voice_by_language={"en": "B"}))
    assert len(tts.calls) == 2 * n


def test_recorded_audio_wins(tmp_path):
    rec = tmp_path / "rec" / "en"
    rec.mkdir(parents=True)
    (rec / "cancelled.wav").write_bytes(pcm_to_wav_bytes(tone_pcm(0.1, 16000), 16000))
    tts = MockTTSProvider()
    out = tmp_path / "out"
    m = run(build_library(tts, out, ["en"], recorded_dir=tmp_path / "rec"))
    assert m["en/cancelled"]["source"] == "recorded" and m["en/cancelled"]["sample_rate"] == 16000
    assert all(c[0] != "Okay, I have cancelled this. No money has moved." for c in tts.calls)
    assert load_manifest(out)["en/cancelled"]["source"] == "recorded"


def test_unreviewed_language_is_generated_from_english_and_labelled(tmp_path):
    m = run(build_library(MockTTSProvider(), tmp_path, ["yo"]))
    assert m["yo/cancelled"]["text_language"] == "en"


def test_reviewed_language_file_is_loaded(tmp_path):
    from avenza_ai.speech.prompts import EN, get_prompts
    f = tmp_path / "ha.json"
    f.write_text(json.dumps({i: f"[ha] {EN[i].text}" for i in PROMPT_IDS}), encoding="utf-8")
    prompts, used = get_prompts("ha", data_dir=tmp_path)
    assert used == "ha" and prompts["cancelled"].claims_no_money_moved
    bad = tmp_path / "ig.json"
    bad.write_text(json.dumps({"cancelled": "x"}))
    try:
        load_prompt_file(bad)
        assert False
    except ValueError as e:
        assert "missing prompt ids" in str(e)


def test_review_sheet(tmp_path):
    p = tmp_path / "review.csv"
    export_review_sheet(p, ["en", "yo"])
    rows = list(csv.reader(open(p, encoding="utf-8")))
    assert rows[0][:3] == ["language", "prompt_id", "text"] and len(rows) == 1 + 2 * len(PROMPT_IDS)
    assert any("English fallback" in r[0] for r in rows[1:])
