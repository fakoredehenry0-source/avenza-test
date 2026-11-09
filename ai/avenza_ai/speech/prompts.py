"""Fixed prompts. Pre-generate and cache them; the frontend plays them locally by id.

Each prompt records WHEN it is truthful. A prompt that says "No money has moved"
must only be played before a transfer was submitted to the bank. After
submission, use error_status_unknown (it makes no claim either way).

Only English and Nigerian Pidgin are shipped. Yoruba, Hausa and Igbo prompts are
loaded from data/prompts/<lang>.json after a native speaker has reviewed them;
until that file exists those languages fall back to English.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

_BEFORE_SUBMIT = "before the transfer is submitted to the bank (states up to AWAITING_BIOMETRIC)"
_ANY = "any state"


@dataclass(frozen=True)
class Prompt:
    id: str
    text: str
    truthful_when: str
    claims_no_money_moved: bool = False


def _mk(rows) -> Dict[str, Prompt]:
    return {r[0]: Prompt(*r) for r in rows}


EN = _mk([
    ("listening_started", "Avenza is listening. Say what you would like to do.", _ANY),
    ("didnt_catch", "Sorry, I did not catch that. Please say it again.", _ANY),
    ("ask_amount", "How much would you like to send?", "drafting"),
    ("ask_recipient", "Who would you like to send it to?", "drafting"),
    ("amount_unclear", "I was not sure about the amount. Please say it again, slowly.", "drafting"),
    ("bank_name_missing", "I could not confirm the account name with the bank. Please say the account details again.", "drafting"),
    ("place_finger", "Place your finger on the sensor to approve.", "AWAITING_BIOMETRIC"),
    ("fingerprint_retry", "I could not read your fingerprint. Please try again.", "AWAITING_BIOMETRIC"),
    ("fingerprint_failed", "I could not read your fingerprint. I have cancelled this. No money has moved.", _BEFORE_SUBMIT, True),
    ("cancelled", "Okay, I have cancelled this. No money has moved.", _BEFORE_SUBMIT, True),
    ("expired", "That request timed out. No money has moved.", _BEFORE_SUBMIT, True),
    ("status_checking", "I am checking the status with your bank. Please wait.", "SUBMITTED or PENDING_UNKNOWN"),
    ("result_failed", "The bank says this transfer did not go through.", "FAILED"),
    ("error_not_moved", "Something went wrong. No money has moved. Please try again.", _BEFORE_SUBMIT, True),
    ("error_offline", "I cannot reach the service right now. No money has moved.", _BEFORE_SUBMIT, True),
    ("error_status_unknown", "I could not confirm the result with your bank. Please check your bank app before trying again.", "SUBMITTED, PENDING_UNKNOWN or unknown"),
    ("error_no_speech", "I did not hear anything. Please try again.", _ANY),
])

PCM = _mk([
    ("listening_started", "Avenza dey listen. Tell me wetin you wan do.", _ANY),
    ("didnt_catch", "Sorry, I no hear am well. Abeg talk am again.", _ANY),
    ("ask_amount", "How much you wan send?", "drafting"),
    ("ask_recipient", "Who you wan send am to?", "drafting"),
    ("amount_unclear", "I no too sure of the amount. Abeg talk am again, slowly.", "drafting"),
    ("bank_name_missing", "I no fit confirm the account name with the bank. Abeg talk the account details again.", "drafting"),
    ("place_finger", "Put your finger for the sensor make you approve am.", "AWAITING_BIOMETRIC"),
    ("fingerprint_retry", "I no fit read your fingerprint. Abeg try again.", "AWAITING_BIOMETRIC"),
    ("fingerprint_failed", "I no fit read your fingerprint. I don cancel am. Money never comot.", _BEFORE_SUBMIT, True),
    ("cancelled", "Ok, I don cancel am. Money never comot.", _BEFORE_SUBMIT, True),
    ("expired", "Time don pass. Money never comot.", _BEFORE_SUBMIT, True),
    ("status_checking", "I dey check with your bank. Abeg wait small.", "SUBMITTED or PENDING_UNKNOWN"),
    ("result_failed", "The bank talk say the transfer no go through.", "FAILED"),
    ("error_not_moved", "Something don happen. Money never comot. Abeg try again.", _BEFORE_SUBMIT, True),
    ("error_offline", "I no fit reach the service now. Money never comot.", _BEFORE_SUBMIT, True),
    ("error_status_unknown", "I no fit confirm the result with your bank. Abeg check your bank app before you try again.", "SUBMITTED, PENDING_UNKNOWN or unknown"),
    ("error_no_speech", "I no hear anything. Abeg try again.", _ANY),
])

CATALOG: Dict[str, Dict[str, Prompt]] = {"en": EN, "pcm": PCM}
PROMPT_IDS = tuple(EN.keys())
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "prompts"


def load_prompt_file(path: Path) -> Dict[str, Prompt]:
    """Native-speaker-reviewed prompts: {"id": "text", ...}. Every English id must be present."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [i for i in PROMPT_IDS if i not in raw]
    if missing:
        raise ValueError(f"{path}: missing prompt ids {missing}")
    return {i: Prompt(i, raw[i], EN[i].truthful_when, EN[i].claims_no_money_moved) for i in PROMPT_IDS}


def get_prompts(language: str, data_dir: Path = DEFAULT_DATA_DIR) -> Tuple[Dict[str, Prompt], str]:
    """Return (prompts, language_actually_used). Falls back to English when a language has no reviewed set."""
    if language in CATALOG:
        return CATALOG[language], language
    f = Path(data_dir) / f"{language}.json"
    if f.exists():
        return load_prompt_file(f), language
    return EN, "en"
