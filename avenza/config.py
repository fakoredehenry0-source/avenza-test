from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    app_name: str = "Avenza"
    version: str = "0.2.0"
    env: str = "development"
    log_level: str = "INFO"

    database_url: str = "sqlite:///./avenza.db"
    bank_mode: str = "mock"

    stt_provider: str = "mock"
    tts_provider: str = "mock"
    allow_text_utterance: bool = True

    yarn_api_key: str = ""
    yarn_asr_url: str = ""
    yarn_tts_url: str = ""
    yarn_tts_voice: str = ""
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-2"
    sahara_api_key: str = ""
    sahara_asr_url: str = ""

    llm_enabled: bool = False
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = ""
    llm_model: str = "llama-3.1-8b-instant"
    llm_timeout_s: float = 1.5

    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "Avenza"
    webauthn_origin: str = "http://localhost:8000"

    max_amount_kobo: int = 100_000_000  # NGN 1,000,000
    large_amount_threshold_kobo: int = 5_000_000  # NGN 50,000
    new_recipient_hold_s: int = 10
    duplicate_window_hours: int = 24
    draft_ttl_s: int = 120
    readback_ttl_s: int = 90
    approval_ttl_s: int = 60
    session_ttl_s: int = 1800

    dev_tools: bool = True
    allow_dev_approval: bool = False
    admin_token: str = ""

    web_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "web")
    prompt_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "web" / "prompts")

    @property
    def bank_is_simulated(self) -> bool:
        return self.bank_mode == "mock"

    @property
    def is_production(self) -> bool:
        return self.env.lower() in {"production", "prod"}

    def for_test(self, **changes) -> "Settings":
        """Return a modified copy, with test-safe defaults."""
        base = {
            "env": "test",
            "database_url": "sqlite:///:memory:",
            "bank_mode": "mock",
            "stt_provider": "mock",
            "tts_provider": "mock",
            "llm_enabled": False,
            "dev_tools": True,
            "allow_dev_approval": True,
            "new_recipient_hold_s": 0,
        }
        base.update(changes)
        return replace(self, **base)


def load_settings() -> Settings:
    return Settings(
        app_name=os.getenv("APP_NAME", "Avenza"),
        env=os.getenv("APP_ENV", "development"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        database_url=os.getenv("DATABASE_URL", "sqlite:///./avenza.db"),
        bank_mode=os.getenv("BANK_MODE", "mock").strip().lower(),
        stt_provider=os.getenv("STT_PROVIDER", "mock").strip().lower(),
        tts_provider=os.getenv("TTS_PROVIDER", "mock").strip().lower(),
        allow_text_utterance=_bool("ALLOW_TEXT_UTTERANCE", True),
        yarn_api_key=os.getenv("YARN_API_KEY", ""),
        yarn_asr_url=os.getenv("YARN_ASR_URL", ""),
        yarn_tts_url=os.getenv("YARN_TTS_URL", ""),
        yarn_tts_voice=os.getenv("YARN_TTS_VOICE", ""),
        deepgram_api_key=os.getenv("DEEPGRAM_API_KEY", ""),
        deepgram_model=os.getenv("DEEPGRAM_MODEL", "nova-2"),
        sahara_api_key=os.getenv("SAHARA_API_KEY", ""),
        sahara_asr_url=os.getenv("SAHARA_ASR_URL", ""),
        llm_enabled=_bool("LLM_ENABLED", False),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL", "llama-3.1-8b-instant"),
        llm_timeout_s=_float("LLM_TIMEOUT_S", 1.5),
        webauthn_rp_id=os.getenv("WEBAUTHN_RP_ID", "localhost"),
        webauthn_rp_name=os.getenv("WEBAUTHN_RP_NAME", "Avenza"),
        webauthn_origin=os.getenv("WEBAUTHN_ORIGIN", "http://localhost:8000"),
        max_amount_kobo=_int("MAX_AMOUNT_KOBO", 100_000_000),
        large_amount_threshold_kobo=_int("LARGE_AMOUNT_THRESHOLD_KOBO", 5_000_000),
        new_recipient_hold_s=_int("NEW_RECIPIENT_HOLD_S", 10),
        duplicate_window_hours=_int("DUPLICATE_WINDOW_HOURS", 24),
        draft_ttl_s=_int("DRAFT_TTL_S", 120),
        readback_ttl_s=_int("READBACK_TTL_S", 90),
        approval_ttl_s=_int("APPROVAL_TTL_S", 60),
        session_ttl_s=_int("SESSION_TTL_S", 1800),
        dev_tools=_bool("DEV_TOOLS", True),
        allow_dev_approval=_bool("ALLOW_DEV_APPROVAL", False),
        admin_token=os.getenv("ADMIN_TOKEN", ""),
        web_dir=PROJECT_ROOT / os.getenv("WEB_DIR", "web"),
        prompt_dir=PROJECT_ROOT / os.getenv("WEB_DIR", "web") / "prompts",
    )
