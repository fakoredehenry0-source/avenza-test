# Handover: what Muiz delivers to Henry (backend) and Abdulwahab (frontend)

## To Henry (backend): import these modules
| Need | Call | Notes |
|---|---|---|
| Understand a turn | `await HybridIntentClassifier(llm=...).classify(transcript, state)` | `state` is the transfer state string; yes/no/repeat only count in `READBACK_PLAYED`, only `cancel` in `AWAITING_BIOMETRIC`, nothing in other states. Empty text is always `UNCLEAR`. |
| Draft a transfer | `await build_draft(transcript, classifier, state=..., languages=enabled, stt_confidence=...)` | Returns `READY`, or `NEEDS_CLARIFICATION` with a `clarify_prompt_id`. **Amount comes only from the parser.** |
| Amount | `parse_amount(text, languages=...)` -> `AmountResult(kobo=int)` | Integer kobo, `status` OK/NONE/AMBIGUOUS/OUT_OF_RANGE. Pass only languages that passed the test. |
| Account number | `parse_account_number(text)` | 10 digits or None. |
| Read-back | `render_readback(kobo, bank_verified_name, bank_name=..., language=...)` | **Pass the bank's name, never the spoken hint.** Raises `ReadbackError` if name/amount missing: then ask, do not speak. |
| Result speech | `render_settled(kobo, name)`, prompt id `result_failed`, `status_checking` | |
| History | `parse_time_phrase(text)` -> range; speak `range.describe()` back | Feeds `list_transactions`. |
| STT | `ProviderChoice.load().stt_for(lang).stream(lang, rate)` | `send_audio`, `finish`, `events()`. Raises `LookupError` for languages that did not pass. |
| TTS | `YarnGPTTTS().synthesize(text, voice, lang)` | CONFIRM items in README. |

Rules I kept, and you must keep:
- The orchestrator, not the classifier, moves states. "Two unclear replies in a row -> CANCELLED" is yours.
- LLM down, rate limited or invalid -> keyword fallback (already built in).
- Log `Timing` from STT (`stream.timing`) and TTS (`TTSResult.timing`) per stage, next to VAD silence tolerance.

## To Abdulwahab (frontend)
- `python -m avenza_ai.eval.vad_eval --log vad_log.json --emit-config vad_config.json` consumes the per-frame
  Silero probabilities you export (format in the module docstring) and writes thresholds, silence tolerance and
  pre-roll. Until real clips exist, use the starting values in `DEFAULTS` and say so.
- Cached prompts: `build_library()` writes `<out>/<lang>/<id>.wav` plus `manifest.json` (id, text, sample rate,
  source recorded/generated). Every id in `PROMPT_IDS` must be playable **offline**. `verify_library()` lists gaps.
- Which error prompt to play locally (honesty rule):
  - Before the transfer was submitted (states up to `AWAITING_BIOMETRIC`): `error_not_moved` / `error_offline` / `cancelled` / `expired`. They say no money has moved.
  - After submission, or if you do not know: `error_status_unknown` only. It makes no claim either way.
- Prompt ids used by the server's `error` message and drafting questions: `didnt_catch, ask_amount, ask_recipient,
  amount_unclear, bank_name_missing, place_finger, fingerprint_retry, fingerprint_failed, cancelled, expired,
  status_checking, result_failed, error_not_moved, error_offline, error_status_unknown, error_no_speech, listening_started`.
