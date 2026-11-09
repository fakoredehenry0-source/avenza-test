import asyncio
import json

import httpx
import pytest

from avenza_ai.providers import (DeepgramSTTProvider, MockSTTProvider, MockTTSProvider, ProviderChoice,
                                 SaharaSTTProvider, YarnGPTSTTProvider, YarnGPTTTS)
from avenza_ai.providers.audio import (decode_audio_response, pcm_to_wav_bytes, tone_pcm, wav_bytes_to_pcm)
from avenza_ai.types import NotConfigured, ProviderError, UnsupportedLanguage

run = asyncio.run
PCM = b"\x01\x00" * 3200


def test_mock_stt_returns_text_and_timing():
    r = run(MockSTTProvider("send five thousand naira").transcribe(PCM, "en", 16000))
    assert r.text == "send five thousand naira"
    assert r.timing.time_to_first_partial_ms is not None and r.timing.time_to_final_ms is not None
    assert any(not e.is_final for e in r.events) and r.events[-1].is_final


def test_mock_stt_failure_is_raised_not_swallowed():
    p = MockSTTProvider("x", fail_with=ProviderError("boom"))
    with pytest.raises(ProviderError):
        run(p.transcribe(PCM, "en", 16000))


def test_mock_stt_unsupported_language():
    p = MockSTTProvider("x", languages={"en"})
    with pytest.raises(UnsupportedLanguage):
        p.stream("yo", 16000)


def test_mock_stt_script_list_and_callable():
    p = MockSTTProvider(["one", "two"])
    assert run(p.transcribe(PCM, "en", 16000)).text == "one"
    assert run(p.transcribe(PCM, "en", 16000)).text == "two"
    q = MockSTTProvider(lambda pcm, lang: f"{lang}:{len(pcm)}")
    assert run(q.transcribe(PCM, "ha", 16000)).text == f"ha:{len(PCM)}"


# ---- Deepgram -------------------------------------------------------------------------
def dg_result(text, final, conf=0.9):
    return json.dumps({"type": "Results", "is_final": final,
                       "channel": {"alternatives": [{"transcript": text, "confidence": conf}]}})


def test_deepgram_parse_messages():
    p = DeepgramSTTProvider(api_key="k")
    assert p.parse_message(dg_result("send five", False))[0].is_final is False
    ev = p.parse_message(dg_result("send five thousand", True))[0]
    assert ev.text == "send five thousand" and ev.is_final and ev.confidence == 0.9
    assert p.parse_message(dg_result("", True)) == []
    assert p.parse_message(json.dumps({"type": "Metadata"})) == []
    assert p.parse_message("not json") == []
    with pytest.raises(ProviderError):
        p.parse_message(json.dumps({"type": "Error", "description": "bad key"}))


def test_deepgram_url_headers_and_languages():
    p = DeepgramSTTProvider(api_key="secret", model="nova-2")
    u = p.url("en", 16000)
    assert "encoding=linear16" in u and "sample_rate=16000" in u and "interim_results=true" in u
    assert p.headers() == {"Authorization": "Token secret"}
    assert p.supports("en") and p.supports("pcm")
    assert not p.supports("yo") and not p.supports("ha") and not p.supports("ig")   # unproven: not assumed
    with pytest.raises(UnsupportedLanguage):
        p.stream("yo", 16000)
    assert json.loads(p.end_message()) == {"type": "CloseStream"}


def test_deepgram_unconfigured_fails_clearly():
    p = DeepgramSTTProvider(api_key="")
    assert not p.is_configured()
    with pytest.raises(NotConfigured):
        run(p.transcribe(PCM, "en", 16000))


def test_deepgram_streaming_against_a_local_websocket_server():
    from websockets.asyncio.server import serve

    received = {"audio": 0, "headers": None, "path": None, "closed": False}

    async def handler(ws):
        received["headers"] = ws.request.headers
        received["path"] = ws.request.path
        async for msg in ws:
            if isinstance(msg, bytes):
                if received["audio"] == 0:
                    await ws.send(dg_result("send five", False))
                received["audio"] += len(msg)
            elif json.loads(msg).get("type") == "CloseStream":
                received["closed"] = True
                await ws.send(dg_result("send five thousand naira", True, 0.93))
                await ws.close()

    async def main():
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            p = DeepgramSTTProvider(api_key="abc", base_url=f"ws://127.0.0.1:{port}/v1/listen")
            return await p.transcribe(PCM, "en", 16000, chunk_ms=50)

    res = run(main())
    assert res.text == "send five thousand naira" and res.confidence == pytest.approx(0.93)
    assert received["audio"] == len(PCM) and received["closed"]
    assert received["headers"].get("Authorization") == "Token abc" and "language=en" in received["path"]
    assert res.timing.time_to_first_partial_ms is not None


def test_deepgram_multiple_finals_are_joined():
    from websockets.asyncio.server import serve

    async def handler(ws):
        async for msg in ws:
            if isinstance(msg, str):
                await ws.send(dg_result("send five thousand", True))
                await ws.send(dg_result("naira to Aisha", True))
                await ws.close()

    async def main():
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            p = DeepgramSTTProvider(api_key="abc", base_url=f"ws://127.0.0.1:{port}/v1/listen")
            return await p.transcribe(PCM, "en", 16000)

    assert run(main()).text == "send five thousand naira to Aisha"


def test_connection_failure_is_a_provider_error():
    p = DeepgramSTTProvider(api_key="abc", base_url="ws://127.0.0.1:9/v1/listen")
    with pytest.raises(ProviderError):
        run(p.transcribe(PCM, "en", 16000, timeout=5))


# ---- YarnGPT / Sahara scaffolds -------------------------------------------------------
def test_scaffold_providers_are_not_configured_until_url_is_set(monkeypatch):
    for k in ("YARNGPT_ASR_WS_URL", "SAHARA_ASR_WS_URL"):
        monkeypatch.delenv(k, raising=False)
    assert not YarnGPTSTTProvider().is_configured() and not SaharaSTTProvider().is_configured()
    assert YarnGPTSTTProvider(url="wss://x.test/asr").is_configured()


def test_scaffold_parse_tolerates_common_shapes():
    p = YarnGPTSTTProvider(url="wss://x.test/asr")
    assert p.parse_message(json.dumps({"transcript": "hi", "is_final": True}))[0].is_final
    assert not p.parse_message(json.dumps({"text": "hi", "final": False}))[0].is_final
    assert p.parse_message(json.dumps({"result": {"text": "hi", "isFinal": True}}))[0].text == "hi"
    assert p.parse_message(json.dumps({"ping": 1})) == []
    with pytest.raises(ProviderError):
        p.parse_message(json.dumps({"error": "quota"}))


# ---- audio helpers --------------------------------------------------------------------
def test_wav_roundtrip_and_decode():
    wav = pcm_to_wav_bytes(PCM, 16000)
    assert wav_bytes_to_pcm(wav) == (PCM, 16000)
    assert decode_audio_response(wav, 24000) == (PCM, 16000)
    assert decode_audio_response(PCM, 24000) == (PCM, 24000)
    with pytest.raises(ProviderError):
        decode_audio_response(b"ID3\x03\x00rest", 24000)


# ---- TTS -------------------------------------------------------------------------------
def tts_with(handler, **kw):
    return YarnGPTTTS(api_key="k", url="http://tts.test/v1/tts", default_voice="V",
                      client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kw)


def test_yarngpt_tts_decodes_wav_and_sends_request():
    seen = {}

    def handler(req):
        seen["json"], seen["auth"] = json.loads(req.read()), req.headers["Authorization"]
        return httpx.Response(200, content=pcm_to_wav_bytes(tone_pcm(0.1, 24000), 24000))

    res = run(tts_with(handler).synthesize("Hello", None, "en"))
    assert res.sample_rate == 24000 and len(res.pcm) == 4800 and res.timing.time_to_first_audio_ms >= 0
    assert seen["json"]["text"] == "Hello" and seen["json"]["voice"] == "V" and seen["auth"] == "Bearer k"


def test_yarngpt_tts_errors():
    with pytest.raises(ProviderError):
        run(tts_with(lambda r: httpx.Response(503)).synthesize("Hi", None, "en"))
    with pytest.raises(ProviderError):
        run(tts_with(lambda r: httpx.Response(200, content=b"ID3....")).synthesize("Hi", None, "en"))
    with pytest.raises(ProviderError):
        run(tts_with(lambda r: httpx.Response(200)).synthesize("   ", None, "en"))
    with pytest.raises(NotConfigured):
        run(YarnGPTTTS(api_key="").synthesize("Hi", None, "en"))


def test_mock_tts_records_calls():
    t = MockTTSProvider()
    r = run(t.synthesize("Hi", "v", "en"))
    assert r.pcm and t.calls == [("Hi", "v", "en")]


# ---- registry ---------------------------------------------------------------------------
def test_registry_only_serves_languages_that_passed(tmp_path):
    f = tmp_path / "choice.json"
    f.write_text(json.dumps({"choice": {"en": "deepgram", "ha": "yarngpt"}, "enabled_languages": ["en"]}))
    c = ProviderChoice.load(str(f))
    assert c.stt_for("en").name == "deepgram"
    with pytest.raises(LookupError):
        c.stt_for("ha")
    default = ProviderChoice.load(str(tmp_path / "missing.json"))
    assert default.enabled_languages == ["en", "pcm"]
