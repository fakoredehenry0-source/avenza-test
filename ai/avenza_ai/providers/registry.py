"""Pick a speech-to-text provider per language from the language-test result."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..types import STTProvider
from .ws_stt import DeepgramSTTProvider, SaharaSTTProvider, YarnGPTSTTProvider

FACTORIES: Dict[str, Callable[[], STTProvider]] = {
    "deepgram": DeepgramSTTProvider,
    "yarngpt": YarnGPTSTTProvider,
    "sahara": SaharaSTTProvider,
}


class ProviderChoice:
    """Reads reports/provider_choice.json. A language is enabled only if it passed the test."""

    def __init__(self, choice: Dict[str, str], enabled_languages: List[str]):
        self.choice, self.enabled_languages = dict(choice), list(enabled_languages)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "ProviderChoice":
        p = Path(path or os.getenv("AVENZA_PROVIDER_CHOICE", "reports/provider_choice.json"))
        if not p.exists():
            return cls({"en": "deepgram", "pcm": "deepgram"}, ["en", "pcm"])  # safe default: English only
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(data.get("choice", {}), data.get("enabled_languages", []))

    def is_enabled(self, language: str) -> bool:
        return language in self.enabled_languages

    def stt_for(self, language: str, factories: Optional[Dict[str, Callable[[], STTProvider]]] = None) -> STTProvider:
        if not self.is_enabled(language):
            raise LookupError(f"{language} has not passed the language test; do not offer it")
        return (factories or FACTORIES)[self.choice[language]]()
