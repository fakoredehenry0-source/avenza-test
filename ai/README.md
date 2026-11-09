# Avenza: AI/ML layer (Adeyemi Muiz)

Voice only drafts a request. Nothing here can move money: the bank-verified name is read back from a template and only the
customer's fingerprint, through the bank, approves a transfer. Design status: **built and unit-tested on mocks; not yet
run against real providers, real audio or real users.** Say that at Demo Day.

## Quick start
```bash
pip install -r requirements.txt
python -m pytest -q                  # 256 tests, no network or API keys needed
python scripts/demo.py               # type a request, see draft + read-back
```
Copy `.env.example` to `.env` and fill keys when testing real providers.

## Map of the doc to the code
| Doc item (section 10.1) | Where |
|---|---|
| Language and latency test | `avenza_ai/eval/lang_test.py`, `data/language_test/manifest.json`, `eval/metrics.py` |
| STT providers behind one interface | `types.py` (interface), `providers/ws_stt.py` (Deepgram, YarnGPT, Sahara), `providers/registry.py`, `providers/mock.py` |
| Deterministic amount parser | `parsing/amount.py`, `parsing/lexicon.py` |
| Time-phrase parser (history) | `parsing/timephrase.py` |
| Intent: LLM + keyword fallback | `intent/` (`keyword.py`, `llm.py`, `classifier.py`, `entities.py`), `draft.py` |
| TTS, number-to-words, read-back, cached prompts | `providers/yarngpt_tts.py`, `speech/numwords.py`, `speech/readback.py`, `speech/prompts.py`, `speech/prompt_library.py` |
| VAD evaluation and tuning | `eval/vad_eval.py` |
| Blind tester outreach | `docs/blind_tester_outreach.md` |
| Hand-overs | `docs/HANDOVER.md` |

## Day 1 checklist (things only you can confirm)
1. **YarnGPT**: confirm the TTS endpoint, auth, request fields, PCM/WAV format and sample rate (`providers/yarngpt_tts.py`),
   and the live-ASR websocket protocol (`providers/ws_stt.py`, class `_GenericWSProvider`). Both are marked CONFIRM and the ASR
   provider reports "not configured" until `YARNGPT_ASR_WS_URL` is set. Same for **Sahara v2.5**.
2. **Record the 40 test sentences** (16 kHz mono WAV, paths in the manifest), then:
   `python -m avenza_ai.eval.lang_test --audio-dir recordings`. Read `reports/report.md`.
   A language only counts as passing with: at least 10 scored samples, at least 90% amounts correct, **0 wrong amounts accepted**, at least 80% names correct.
3. **Native-speaker review** of: the Yoruba/Hausa/Igbo reference sentences, the number words in `parsing/lexicon.py`,
   the local yes/no/cancel/transfer words in `intent/keyword.py`, and any prompts for those languages
   (`export_review_sheet()` makes the CSV; reviewed prompts go in `data/prompts/<lang>.json`).
4. Deepgram is only wired for English/Pidgin. It may not cover Yoruba, Hausa or Igbo; the test decides, nothing is assumed.

## Safety properties (all covered by tests)
- Amounts come only from `parse_amount`. The LLM's output is label + a name copied from the transcript; anything else is discarded.
- Ambiguity is rejected, never guessed: "fifteen fifty", "5000 or 7000 naira", kobo, two amounts, out-of-range, odd local-language forms.
- Account and phone numbers (10+ digits or a long spoken digit run) are never amounts. "five k" means 5,000, not 5.
- Silence or empty text never produces an intent. Conflicting words ("yes no", "not correct") are unclear. Long speech while confirming is unclear.
- Read-back uses the bank name in full and adds the digits when speech could be mistaken (thirteen/thirty ... nineteen/ninety).
- Prompts that say "no money has moved" are tagged as valid only before submission; a test enforces this.
- Local number words are off unless you enable that language; `AmountResult.needs_review` flags their use.

## Known limits (honest list)
- Yoruba numbers above the supported set, Igbo compounds like "puku iri na ise", and anything irregular are **rejected**, so those customers get "please say it again" until a native speaker extends the grammar.
- Number-to-words exists for English (and Pidgin) only; Yoruba/Hausa/Igbo read-backs fall back to English until reviewed.
- Pidgin is not in the language test set (the doc lists four languages); treat it as unproven.
- VAD tuning needs the frontend's recorded probability log; the synthetic data in tests only proves the tool works.
- Vendor accuracy and latency figures are unverified until `lang_test` produces them.
