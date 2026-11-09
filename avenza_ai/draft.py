"""Turn a transcript into a draft, or into the one question to ask next.

Order of authority: the intent classifier says WHAT the customer wants; only the
deterministic parser says HOW MUCH; the bank says WHO (name enquiry). If anything is
missing or unclear, the result asks a question instead of guessing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .intent.entities import extract_bank_hint, extract_recipient_hint, parse_account_number
from .parsing.amount import AmountResult, AmountStatus, parse_amount
from .types import Intent, IntentClassifier, IntentResult

READY = "ready"
NEEDS_CLARIFICATION = "needs_clarification"
NOT_A_TRANSFER = "not_a_transfer"


@dataclass(frozen=True)
class DraftResult:
    status: str
    intent: IntentResult
    amount: Optional[AmountResult] = None
    recipient_hint: Optional[str] = None
    account_number: Optional[str] = None
    bank_hint: Optional[str] = None
    clarify_prompt_id: Optional[str] = None   # a prompt id from speech.prompts


async def build_draft(transcript: str, classifier: IntentClassifier, *, state: Optional[str] = None,
                      languages: Iterable[str] = ("en",), stt_confidence: Optional[float] = None,
                      min_confidence: float = 0.6) -> DraftResult:
    intent = await classifier.classify(transcript, state)
    if intent.intent is not Intent.TRANSFER:
        return DraftResult(NOT_A_TRANSFER, intent)
    if stt_confidence is not None and stt_confidence < min_confidence:
        return DraftResult(NEEDS_CLARIFICATION, intent, clarify_prompt_id="didnt_catch")
    amount = parse_amount(transcript, languages=languages)
    name = intent.fields.get("recipient_name") or extract_recipient_hint(transcript)
    bank = intent.fields.get("bank") or extract_bank_hint(transcript)
    account = parse_account_number(transcript)
    common = dict(intent=intent, amount=amount, recipient_hint=name, account_number=account, bank_hint=bank)
    if amount.status is AmountStatus.NONE:
        return DraftResult(NEEDS_CLARIFICATION, clarify_prompt_id="ask_amount", **common)
    if not amount.ok:
        return DraftResult(NEEDS_CLARIFICATION, clarify_prompt_id="amount_unclear", **common)
    if not name and not account:
        return DraftResult(NEEDS_CLARIFICATION, clarify_prompt_id="ask_recipient", **common)
    return DraftResult(READY, **common)
