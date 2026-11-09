# AI/ML integration

This backend now contains the AI/ML engineer's complete `avenza_ai` package and
wires it into the runtime behind the existing backend contracts. The AI code was
**adapted, not merged blindly**, and the money-movement invariant is unchanged.

> Voice only drafts a transfer. Only a WebAuthn assertion verified by the backend
> can move money. The AI layer cannot approve anything.

## What is wired in

| AI package module | Backend integration | Default |
| --- | --- | --- |
| `avenza_ai.intent.HybridIntentClassifier` + `LlamaIntentClassifier` | `avenza.ai.workflow.AIWorkflow.classify` augments the backend classifier when the backend is genuinely unsure. Backend `LOCK`, `HELP`, `YES`, `NO`, `REPEAT`, `CANCEL` always win. | on (`AI_ENABLED`, `AI_INTENT_ENABLED`) |
| `avenza_ai.parsing.amount.parse_amount` | Optional cross-check only. If it disagrees with `avenza.parser`, the orchestrator asks the customer to repeat. It never supplies the amount. | off (`AI_AMOUNT_CROSSCHECK`) |
| `avenza_ai.speech.readback.render_readback` | Optional read-back template using the bank-verified name and digit spelling for 13/30-style confusions. Falls back to the backend template. | off (`AI_READBACK`) |
| `avenza_ai.parsing.timephrase.parse_time_phrase` | Adds history phrases such as "in October", "this year" and "5 days ago" when the backend falls back to its 30-day window. | on (`AI_HISTORY`) |
| `avenza_ai.providers` (`ProviderChoice`, Deepgram / YarnGPT / Sahara streams) | `avenza.providers.ai_speech.AIWebSocketSTT`, selectable with `STT_PROVIDER=ai`. Falls back to mock when no credentials exist. | mock |
| `avenza_ai.providers.yarngpt_tts.YarnGPTTTS` | `avenza.providers.ai_speech.AIYarnTTS`, selectable with `TTS_PROVIDER=ai` or `yarngpt`. | mock |
| `avenza_ai.eval.*` | Kept as runnable evaluation tools and tests (`tests/ai/`). | tools only |

## Safety invariants kept

1. `avenza.parser` remains the only amount authority. The AI parser is a
   cross-check that can only force a clarification.
2. `AWAITING_BIOMETRIC → APPROVED` still requires a backend-verified WebAuthn
   assertion. No AI path writes a transfer state.
3. The read-back always uses the bank's name-enquiry result, never a spoken hint.
4. AI intent results are gated by state. Confirmations are never delegated to a
   model.
5. If the AI layer is unavailable, the deterministic backend layer continues to
   work. `AI_ENABLED=false` restores the pre-AI behaviour exactly.

## Runtime switches

| Variable | Default | Meaning |
| --- | --- | --- |
| `AI_ENABLED` | `true` | Master switch for the AI bridge. |
| `AI_INTENT_ENABLED` | `true` | Use the hybrid AI classifier for unclear paraphrases. |
| `AI_AMOUNT_CROSSCHECK` | `false` | Ask again if the AI parser disagrees with the backend parser. |
| `AI_READBACK` | `false` | Use the AI read-back template. |
| `AI_HISTORY` | `true` | Use AI time phrases for history. |
| `AI_LANGUAGES` | `en` | Languages whose number/intent words are enabled. |
| `AI_PROVIDER_CHOICE` | `reports/provider_choice.json` | Language-to-provider map written by the language test. |
| `STT_PROVIDER=ai` | `mock` | Use the AI streaming STT registry. |
| `TTS_PROVIDER=ai` | `mock` | Use YarnGPT TTS through the AI package. |

Only English is currently claimed as verified. Do not add `yo`, `ha` or `ig` to
`AI_LANGUAGES` until the language test has passed and a native speaker has
reviewed the number words and prompts.

## Running the AI tests and tools

```powershell
python -m pytest tests/ai -q          # 256 AI/ML tests, no network
python -m pytest -q                   # full backend + AI suite
python scripts/ai_demo.py             # keyboard walk-through of the AI pipeline
python -m avenza_ai.eval.lang_test --help
python -m avenza_ai.eval.vad_eval --help
```

The language test needs recorded audio. Its manifest lives at
`data/language_test/manifest.json`; the command writes
`reports/provider_choice.json`, which `STT_PROVIDER=ai` reads.

## Branch and merge guidance

The AI package was delivered on `feat/avenza-v1-ml`. Do **not** merge that branch
directly on top of the backend and hope conflicts resolve themselves. The
backend is the integration base because it owns the state machine, WebAuthn
approval, audit chain and money handling.

Recommended sequence:

```bash
git checkout feat/avenza-backend-v1
git checkout -b feat/avenza-integrated
git merge --no-ff feat/avenza-v1-ml
# resolve conflicts in favour of the backend for:
#   avenza/state_machine.py, avenza/services/transfers.py,
#   avenza/security.py, avenza/parser.py (amount authority),
#   avenza/orchestrator.py (approval path)
python -m pytest -q
```

The result should look like this repository: `avenza/` remains authoritative,
`avenza_ai/` is vendored, and `avenza/ai/` is the only bridge. After the suite
passes, package and tag the integrated branch.
