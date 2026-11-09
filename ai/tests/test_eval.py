import asyncio
import json
import wave
from pathlib import Path

import pytest

from avenza_ai.eval import lang_test, metrics, vad_eval
from avenza_ai.eval.dataset import load_manifest
from avenza_ai.parsing import parse_amount
from avenza_ai.providers import MockSTTProvider
from avenza_ai.providers.audio import tone_pcm

MANIFEST = lang_test.DEFAULT_MANIFEST


# ---- metrics ---------------------------------------------------------------------------
def test_wer():
    assert metrics.word_error_rate("send five thousand naira", "send five thousand naira") == 0
    assert metrics.word_error_rate("send fifteen thousand", "send fifty thousand") == pytest.approx(1 / 3)
    assert metrics.word_error_rate("a b c d", "") == 1
    assert metrics.word_error_rate("", "") == 0
    assert metrics.word_error_rate("Ẹgbẹ̀rún márùn-ún", "egberun marunun") == 0   # tone marks do not count


def test_amount_outcomes():
    f = lambda exp, hyp: metrics.amount_outcome(exp, hyp, ("yo", "ha", "ig"))  # noqa: E731
    assert f(1500000, "send fifteen thousand naira") == "correct"
    assert f(1500000, "send fifty thousand naira") == "wrong"          # the dangerous one
    assert f(1500000, "send fifteen fifty naira") == "rejected"
    assert f(1500000, "send money") == "rejected"


def test_name_match():
    assert metrics.name_correct("Aisha Bello", "send 5000 to aisha bello") is True
    assert metrics.name_correct("Aisha Bello", "send 5000 to Aisha Belo") is False
    assert metrics.name_correct("Aisha Bello-Okafor", "to aisha bello") is False
    assert metrics.name_correct(None, "anything") is None


def test_percentiles():
    assert metrics.median([1, 2, 3]) == 2 and metrics.median([]) is None
    assert metrics.percentile([1, 2, 3, 4, 5], 90) == pytest.approx(4.6)


# ---- the manifest is sound and its references pass the parser --------------------------
def test_manifest_has_ten_per_language_and_unique_ids():
    s = load_manifest(MANIFEST)
    assert len(s) == 40 and len({x.id for x in s}) == 40
    for lang in ("en", "yo", "ha", "ig"):
        assert sum(x.language == lang for x in s) == 10
    for x in s:
        assert x.expected_name and x.expected_kobo and x.audio_file.startswith(x.language + "/")


def test_manifest_covers_required_conditions():
    tags = {t for x in load_manifest(MANIFEST) for t in x.tags}
    assert {"noise", "code_switch", "teen_ty", "similar_name"} <= tags


def test_reference_sentences_parse_to_their_expected_amount():
    for x in load_manifest(MANIFEST):
        r = parse_amount(x.text, languages=("yo", "ha", "ig"))
        assert r.ok and r.kobo == x.expected_kobo, (x.id, x.text, r)


def test_local_reference_sentences_need_the_language_enabled():
    ha = next(x for x in load_manifest(MANIFEST) if x.id == "ha_01")
    assert not parse_amount(ha.text).ok


# ---- runner end to end with mock providers ----------------------------------------------
def write_audio(root: Path, samples):
    for s in samples:
        p = root / s.audio_file
        p.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(tone_pcm(0.2, 16000))


def perfect(samples):
    ref = {}
    it = iter(samples)

    def script(pcm, lang):
        return next(it).text
    return script


def test_runner_picks_passing_providers_and_blocks_failures(tmp_path):
    samples = load_manifest(MANIFEST)
    write_audio(tmp_path / "audio", samples)
    by_text = {}

    def good(pcm, lang):             # perfect transcripts
        return good.queue.pop(0)
    good.queue = [s.text for s in samples]

    def bad_amount(pcm, lang):       # always turns fifteen into fifty (accepted by the parser!)
        t = bad_amount.queue.pop(0)
        return t.replace("fifteen", "fifty")
    bad_amount.queue = [s.text for s in samples]

    providers = [MockSTTProvider(good, name="good"), MockSTTProvider(bad_amount, name="bad"),
                 MockSTTProvider("x", name="en_only", languages={"en"}),
                 MockSTTProvider("x", name="nokey")]
    providers[3].is_configured = lambda: False
    rows = asyncio.run(lang_test.run_samples(samples, providers, tmp_path / "audio"))
    summary = lang_test.summarise(rows)
    assert summary[("good", "yo")]["amount_correct"] == 1 and summary[("good", "yo")]["wer"] == 0
    assert summary[("en_only", "ha")]["scored"] == 0 and "unsupported_language" in summary[("en_only", "ha")]["statuses"]
    assert summary[("nokey", "en")]["statuses"] == ["not_configured"]
    assert summary[("bad", "en")]["amount_wrong"] > 0
    result = lang_test.write_reports(rows, tmp_path / "reports")
    assert result["choice"]["en"] == "good" and set(result["enabled_languages"]) >= {"en", "yo", "ha", "ig", "pcm"}
    assert (tmp_path / "reports" / "report.md").read_text().count("PASS") >= 4
    saved = json.loads((tmp_path / "reports" / "provider_choice.json").read_text())
    assert saved["choice"]["ha"] in ("good", "bad")   # both perfect in Hausa; latency breaks the tie


def test_language_fails_when_no_provider_covers_it(tmp_path):
    samples = [s for s in load_manifest(MANIFEST) if s.language in ("en", "yo")]
    write_audio(tmp_path / "audio", samples)
    q = [s.text for s in samples if s.language == "en"]
    p = MockSTTProvider(lambda pcm, lang: q.pop(0), languages={"en"}, name="en_only")
    rows = asyncio.run(lang_test.run_samples(samples, [p], tmp_path / "audio"))
    res = lang_test.write_reports(rows, tmp_path / "r")
    assert "yo" in res["not_passed"] and "yo" not in res["enabled_languages"] and "en" in res["enabled_languages"]
    assert "Do not demo or claim this language" in (tmp_path / "r" / "report.md").read_text()


def test_missing_audio_and_provider_errors_are_recorded_not_hidden(tmp_path):
    samples = load_manifest(MANIFEST)[:2]
    rows = asyncio.run(lang_test.run_samples(samples, [MockSTTProvider("x")], tmp_path))
    assert {r["status"] for r in rows} == {"missing_audio"}
    write_audio(tmp_path, samples)
    from avenza_ai.types import ProviderError
    rows = asyncio.run(lang_test.run_samples(samples, [MockSTTProvider("x", fail_with=ProviderError("down"))], tmp_path))
    assert {r["status"] for r in rows} == {"error"} and "down" in rows[0]["error"]


def test_too_few_samples_never_passes(tmp_path):
    samples = [s for s in load_manifest(MANIFEST) if s.language == "en"][:3]
    write_audio(tmp_path, samples)
    q = [s.text for s in samples]
    rows = asyncio.run(lang_test.run_samples(samples, [MockSTTProvider(lambda p, l: q.pop(0))], tmp_path))
    assert lang_test.write_reports(rows, tmp_path / "r")["enabled_languages"] == []


# ---- VAD --------------------------------------------------------------------------------
def test_vad_simulation_basic():
    probs = [0.0] * 10 + [0.9] * 20 + [0.0] * 40
    segs = vad_eval.simulate(probs, 32, vad_eval.VadParams(silence_ms=500))
    assert len(segs) == 1 and segs[0][0] == 320 and segs[0][1] == pytest.approx(30 * 32)


def test_vad_short_spike_is_ignored_and_pause_is_tolerated():
    spike = [0.0] * 10 + [0.9] * 3 + [0.0] * 30
    assert vad_eval.simulate(spike, 32, vad_eval.VadParams(min_speech_ms=250)) == []
    paused = [0.9] * 20 + [0.0] * 10 + [0.9] * 20 + [0.0] * 40         # 320 ms pause
    assert len(vad_eval.simulate(paused, 32, vad_eval.VadParams(silence_ms=800))) == 1
    assert len(vad_eval.simulate(paused, 32, vad_eval.VadParams(silence_ms=200))) == 2


def test_vad_longer_silence_tolerance_cuts_off_less():
    clips = vad_eval.synthesize_clips()
    short = vad_eval.score(clips, 32, vad_eval.VadParams(silence_ms=200))
    long_ = vad_eval.score(clips, 32, vad_eval.VadParams(silence_ms=1200))
    assert short["cutoff_rate"] > long_["cutoff_rate"]
    assert long_["cutoff_rate"] <= 0.05


def test_vad_sweep_picks_smallest_tolerance_within_limits():
    clips = vad_eval.synthesize_clips()
    best, table = vad_eval.sweep(clips, 32)
    assert best and best["meets_limits"]
    assert best["silence_ms"] == min(r["silence_ms"] for r in table if r["meets_limits"])
    cfg = vad_eval.recommend_config(best)
    assert cfg["pre_roll_ms"] >= 100 and cfg["browser_option_names"]["silence_ms"] == "redemptionMs"


def test_vad_sweep_reports_failure_when_limits_are_impossible():
    clips = vad_eval.synthesize_clips()
    best, _ = vad_eval.sweep(clips, 32, max_false_trigger=0.0, max_miss=0.0, max_cutoff=0.0, silences=(100,))
    assert best is None


def test_vad_cli(tmp_path):
    log = tmp_path / "log.json"
    log.write_text(json.dumps({"frame_ms": 32, "clips": vad_eval.synthesize_clips()}))
    out = tmp_path / "cfg.json"
    assert vad_eval.main(["--log", str(log), "--emit-config", str(out), "--report", str(tmp_path / "r.json")]) == 0
    assert json.loads(out.read_text())["silence_ms"] >= 400
