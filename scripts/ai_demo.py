"""Try the pipeline from the keyboard, with a conversation that remembers the draft.

    python scripts/ai_demo.py
    python scripts/ai_demo.py --lang ha        # enables Hausa number words (unverified)

This stands in for the backend orchestrator (Henry's part): it keeps the pending draft and the
transfer state, and passes the state to the classifier so 'yes' only counts after a read-back.
It cannot move money. In the real flow the NAME comes from the bank; here we echo what you typed.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avenza_ai.draft import READY, build_draft  # noqa: E402
from avenza_ai.intent import HybridIntentClassifier  # noqa: E402
from avenza_ai.intent.entities import parse_account_number  # noqa: E402
from avenza_ai.parsing import parse_amount  # noqa: E402
from avenza_ai.speech import get_prompts, render_readback  # noqa: E402
from avenza_ai.states import READBACK_PLAYED  # noqa: E402
from avenza_ai.types import Intent  # noqa: E402


async def main(lang: str) -> None:
    langs = ("en",) if lang in ("en", "pcm") else ("en", lang)
    prompts = get_prompts(lang)[0]
    say = lambda pid: print("  avenza says:", prompts[pid].text)  # noqa: E731
    clf = HybridIntentClassifier()
    state, draft, ask, unclear = None, {}, None, 0   # state None = no active transfer
    print("Type what a customer would say, e.g. 'send fifteen thousand naira to Aisha Bello'.")
    print("Answer questions naturally ('10000', 'Aisha Bello', 'yes'). Ctrl-C to quit.")

    def readback():
        rb = render_readback(draft["kobo"], draft["name"] or "Account holder", bank_name=draft.get("bank"), language=lang)
        print("  avenza says:", rb.text)

    while True:
        text = input("customer> ")

        if state == READBACK_PLAYED:                      # waiting for yes / no / repeat
            r = await clf.classify(text, state)
            if r.intent is Intent.YES:
                print("  [state] CONFIRMED_BY_VOICE -> AWAITING_BIOMETRIC")
                say("place_finger")
                print("  (real app: fingerprint approval happens in the browser and backend. Demo ends this transfer.)")
                state, draft, ask, unclear = None, {}, None, 0
            elif r.intent is Intent.NO:
                print("  [state] CANCELLED")
                say("cancelled")
                state, draft, ask, unclear = None, {}, None, 0
            elif r.intent is Intent.REPEAT:
                unclear = 0
                readback()
            else:
                unclear += 1
                if unclear >= 2:
                    print("  [state] CANCELLED (two unclear replies in a row)")
                    say("cancelled")
                    state, draft, ask, unclear = None, {}, None, 0
                else:
                    say("didnt_catch")
            continue

        if ask in ("ask_amount", "amount_unclear"):      # we asked "how much?"
            a = parse_amount(text, languages=langs)
            if a.ok:
                draft["kobo"], ask = a.kobo, None
            else:
                say("amount_unclear" if a.reason != "no_amount" else "ask_amount")
                continue
        elif ask == "ask_recipient":                      # we asked "who?"
            acct = parse_account_number(text)
            name = None if acct else text.strip()
            if acct or (name and any(c.isalpha() for c in name)):
                draft["account"], draft["name"], ask = acct, name, None
            else:
                say("ask_recipient")
                continue
        else:                                             # a fresh request
            d = await build_draft(text, clf, languages=langs)
            if d.status != READY and d.intent.intent is not Intent.TRANSFER:
                print("  intent:", d.intent.intent.value, "- nothing to do (try: send 5000 naira to Aisha Bello)")
                continue
            draft = {"kobo": d.amount.kobo if d.amount and d.amount.ok else None, "name": d.recipient_hint,
                     "account": d.account_number, "bank": d.bank_hint}
            ask = d.clarify_prompt_id if d.status != READY else None

        # what is still missing?
        if draft["kobo"] is None:
            ask = ask or "ask_amount"
            say(ask)
        elif not draft["name"] and not draft["account"]:
            ask = "ask_recipient"
            say(ask)
        else:
            ask, state, unclear = None, READBACK_PLAYED, 0
            print("  [state] DRAFTED -> READBACK_PLAYED | amount:", draft["kobo"], "kobo | account:", draft.get("account"))
            readback()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="en")
    try:
        asyncio.run(main(ap.parse_args().lang))
    except (KeyboardInterrupt, EOFError):
        pass