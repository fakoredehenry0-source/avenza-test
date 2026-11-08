from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

# Fixed prompt library. Translations are deliberately marked unverified: the
# documentation says no language is claimed before it passes the language test.
PROMPTS: dict[str, dict[str, str]] = {
    "en": {
        "listening": "Listening. Say what you want to do.",
        "listening_resume": "Listening again.",
        "session_ready": "Avenza is ready. Say a payment request.",
        "ask_amount": "How much do you want to send?",
        "ask_account": "Which account number should I send to?",
        "ask_bank": "Which bank is that account with?",
        "ask_all": "Tell me the amount, the account number, and the bank.",
        "clarify_amount": "I could not hear one clear amount. Please say the amount again. The money has not moved.",
        "clarify_account": "I could not hear one clear ten digit account number. Please say it again. The money has not moved.",
        "no_speech": "I did not hear you. The money has not moved.",
        "unclear": "I did not understand. The money has not moved.",
        "readback": "You are about to send {amount_words}, that is {amount_display}, to {recipient}, account ending {last4}, {bank}. Say yes to continue, no to cancel, or repeat.",
        "duplicate_warning": "This looks like a payment you made recently to the same account for the same amount.",
        "new_recipient_warning": "This is a new recipient and a large amount. Please take a moment to check it.",
        "name_mismatch_warning": "The bank's verified name is {recipient}. I heard {hint}. The bank's name is the one I will use.",
        "confirm_voice": "Thank you. Place your finger on the sensor to approve this exact payment.",
        "fingerprint_retry": "I could not read your fingerprint. Try again, or say cancel.",
        "approval_expired": "The fingerprint approval expired. No money moved.",
        "approved": "Fingerprint approved. Sending to the bank.",
        "settled": "Payment settled. {amount_display} to {recipient}. Reference {reference}.",
        "failed": "Payment failed. No money moved. {reason}",
        "pending": "The bank has not confirmed yet. I will check the status. Do not send again.",
        "cancelled": "Cancelled. No money moved.",
        "expired": "This draft expired. No money moved.",
        "locked": "Transfers are locked. Only your fingerprint can unlock them.",
        "unlock_prompt": "Place your finger on the sensor to unlock transfers.",
        "error_generic": "Something went wrong. The money has not moved.",
        "error_network": "I cannot reach the bank right now. The money has not moved.",
        "error_stt": "I cannot process speech right now. The money has not moved.",
        "error_tts": "I cannot speak the read-back right now. The money has not moved.",
        "error_bank": "The bank is unavailable. The money has not moved.",
        "error_timeout": "That took too long. No money moved. Please try again.",
        "history_summary": "You have {count} transactions in {period}. {summary}",
        "history_empty": "I found no transactions in {period}.",
        "receipt": "Receipt for {amount_display} to {recipient}. Status {status}. Reference {reference}.",
        "help": "I can send money by voice. Say, send five thousand naira to GTBank account zero one two three four five six seven eight nine. Voice only drafts. Your fingerprint approves.",
        "consent_required": "Please record your consent before we start.",
        "device_required": "This device is not enrolled. Please enrol your fingerprint first.",
        "transfer_locked": "Transfers are locked. No money moved.",
        "too_many_attempts": "Too many unclear replies. I cancelled this draft. No money moved.",
        "approval_required": "Voice cannot move money. Use your fingerprint.",
        "unsupported_language": "This language has not passed our test yet. I will continue in English.",
        "amount_too_large": "That amount is above the limit of {limit}. No money moved. Say a smaller amount.",
        "risk_hold": "Please wait {seconds} seconds before I ask for your fingerprint.",
        "draft_active": "You already have a draft. Say yes to continue, no to cancel, or repeat.",
        "wait_status": "Please wait while I check the bank.",
        "no_draft": "There is no draft. Say a payment request.",
        "name_not_found": "I could not find that account at {bank}. Please say the account number again. No money moved.",
        "history_receipt_missing": "I found no receipt to read.",
        "unlock_success": "Transfers unlocked.",
        "unlock_failed": "I could not verify your fingerprint. Transfers remain locked.",
        "voice_biometric": "Use your fingerprint to approve. Voice cannot move money.",
        "session_expired": "This session expired. Please start again. No money moved.",
        "payment_in_progress": "A payment is already in progress. Please wait.",
    },
    "yo": {
        "listening": "Mo ń gbọ́. Sọ ohun tí o fẹ́ ṣe.",
        "listening_resume": "Mo ń gbọ́ lẹ́ẹ̀kan sí.",
        "session_ready": "Avenza ti ṣetán. Sọ ìbéèrè ìsanwó.",
        "ask_amount": "Iye mélòó ni o fẹ́ fi ránṣẹ́?",
        "ask_account": "Nọ́mbà àkọsílẹ̀ wo ni kí n fi ránṣẹ́?",
        "ask_bank": "Ilé-ìfowópamọ́ wo ni àkọsílẹ̀ yẹn wà?",
        "ask_all": "Sọ iye, nọ́mbà àkọsílẹ̀, àti ilé-ìfowópamọ́.",
        "clarify_amount": "Mi ò gbọ́ iye kan ṣoṣo. Jọ̀wọ́ sọ iye náà lẹ́ẹ̀kan sí. Owó kò tíì lọ.",
        "clarify_account": "Mi ò gbọ́ nọ́mbà àkọsílẹ̀ mẹ́wàá kan ṣoṣo. Jọ̀wọ́ sọ ọ́ lẹ́ẹ̀kan sí. Owó kò tíì lọ.",
        "no_speech": "Mi ò gbọ́ ọ. Owó kò tíì lọ.",
        "unclear": "Mi ò yé mi. Owó kò tíì lọ.",
        "readback": "Ìwọ ń fẹ́ fi {amount_words} ránṣẹ́, ìyẹn {amount_display}, sí {recipient}, àkọsílẹ̀ tó parí pẹ̀lú {last4}, {bank}. Sọ bẹ́ẹ̀ láti tẹ̀síwájú, rárá láti fagilé, tàbí sọ lẹ́ẹ̀kan sí.",
        "duplicate_warning": "Èyí dà bí ìsanwó tí o ṣe láìpẹ́ sí àkọsílẹ̀ kan náà fún iye kan náà.",
        "new_recipient_warning": "Olùgbà tuntun ni, iye náà sì tóbi. Jọ̀wọ́ yẹ̀wò ó.",
        "name_mismatch_warning": "Orúkọ tí ilé-ìfowópamọ́ fọwọ́sí ni {recipient}. Mo gbọ́ {hint}. Orúkọ ilé-ìfowópamọ́ ni màá lò.",
        "confirm_voice": "Ẹ ṣé. Fi ìka rẹ sí ojú ẹ̀rọ láti fọwọ́sí ìsanwó yìí.",
        "fingerprint_retry": "Mi ò kà ìka rẹ. Gbìyànjú lẹ́ẹ̀kan sí, tàbí sọ fagilé.",
        "approval_expired": "Àkókò ìfọwọ́sí ti parí. Owó kò lọ.",
        "approved": "Ìka ti fọwọ́sí. Ń fi ránṣẹ́ sí ilé-ìfowópamọ́.",
        "settled": "Ìsanwó ti parí. {amount_display} sí {recipient}. Nọ́mbà ìtọ́kasí {reference}.",
        "failed": "Ìsanwó kùnà. Owó kò lọ. {reason}",
        "pending": "Ilé-ìfowópamọ́ kò tíì jẹ́rìí. Màá yẹ̀wò. Má tún ránṣẹ́.",
        "cancelled": "A fagilé. Owó kò lọ.",
        "expired": "Àkókò parí. Owó kò lọ.",
        "locked": "Àwọn ìsanwó ti dí. Ìka rẹ nìkan ló lè ṣí i.",
        "unlock_prompt": "Fi ìka rẹ sí ojú ẹ̀rọ láti ṣí àwọn ìsanwó.",
        "error_generic": "Nǹkan kan ṣàṣìṣe. Owó kò lọ.",
        "error_network": "Mi ò lè dé ilé-ìfowópamọ́. Owó kò lọ.",
        "error_stt": "Mi ò lè ṣiṣẹ́ lórí ọ̀rọ̀. Owó kò lọ.",
        "error_tts": "Mi ò lè sọ ìkàwé náà. Owó kò lọ.",
        "error_bank": "Ilé-ìfowópamọ́ kò sí. Owó kò lọ.",
        "error_timeout": "Ó gba àkókò púpọ̀. Owó kò lọ. Gbìyànjú lẹ́ẹ̀kan sí.",
        "history_summary": "O ní ìsanwó {count} ní {period}. {summary}",
        "history_empty": "Mi ò rí ìsanwó kankan ní {period}.",
        "receipt": "Ìwé ẹ̀rí fún {amount_display} sí {recipient}. Ipò {status}. Nọ́mbà {reference}.",
        "help": "Mo lè fi ohùn ránṣẹ́ owó. Sọ, fi ẹgbẹ̀rún márùn-ún náírà ránṣẹ́ sí GTBank àkọsílẹ̀ zero one two three four five six seven eight nine. Ohùn ń ṣe àdáwò. Ìka rẹ ló fọwọ́sí.",
        "consent_required": "Jọ̀wọ́ gba àṣẹ kí a tó bẹ̀rẹ̀.",
        "device_required": "A kò tíì forúkọ ẹ̀rọ yìí. Jọ̀wọ́ forúkọ ìka rẹ.",
        "transfer_locked": "Àwọn ìsanwó ti dí. Owó kò lọ.",
        "too_many_attempts": "Ìdáhùn tí kò yé mi púpọ̀. Mo fagilé àdáwò yìí. Owó kò lọ.",
        "approval_required": "Ohùn kò lè gbé owó. Lo ìka rẹ.",
        "unsupported_language": "Èdè yìí kò tíì kọjá ìdánwò wa. Màá tẹ̀síwájú ní Gẹ̀ẹ́sì.",
    },
    "ha": {
        "listening": "Ina jin. Fa abin da kake so.",
        "listening_resume": "Ina jin kuma.",
        "session_ready": "Avenza a shirye. Fa bukatar biya.",
        "ask_amount": "Nawa kake so ka aika?",
        "ask_account": "Wane lambar asusu zan aika zuwa?",
        "ask_bank": "Wane banki ne wannan asusun?",
        "ask_all": "Fa adadin, lambar asusu, da banki.",
        "clarify_amount": "Ban ji adadi daya bayyananne ba. Don Allah sake fadin adadin. Kudin bai motsa ba.",
        "clarify_account": "Ban ji lambar asusu mai lambobi goma ba. Don Allah sake fada. Kudin bai motsa ba.",
        "no_speech": "Ban jinka ba. Kudin bai motsa ba.",
        "unclear": "Ban gane ba. Kudin bai motsa ba.",
        "readback": "Za ka aika {amount_words}, wato {amount_display}, zuwa {recipient}, asusu mai karshen {last4}, {bank}. Ka ce i, don ci gaba, a'a don soke, ko sake fada.",
        "duplicate_warning": "Wannan yayi kama da biyan da ka yi kwanan nan zuwa asusu daya da adadi daya.",
        "new_recipient_warning": "Sabon mai karba ne kuma adadi ya yi yawa. Don Allah ka duba.",
        "name_mismatch_warning": "Sunan da banki ya tabbatar shine {recipient}. Na ji {hint}. Sunan banki zan yi amfani da shi.",
        "confirm_voice": "Na gode. Sanya yatsanka a na'urar don amincewa da wannan biyan.",
        "fingerprint_retry": "Ban karanta yatsanka ba. Sake gwadawa, ko ka ce soke.",
        "approval_expired": "Lokacin amincewa ya kare. Kudin bai motsa ba.",
        "approved": "Yatsa ya amince. Ana aika zuwa banki.",
        "settled": "Biyan ya tabbata. {amount_display} zuwa {recipient}. Lambar {reference}.",
        "failed": "Biyan ya kasa. Kudin bai motsa ba. {reason}",
        "pending": "Banki bai tabbatar ba tukuna. Zan duba. Kada ka sake aika.",
        "cancelled": "An soke. Kudin bai motsa ba.",
        "expired": "Lokaci ya kare. Kudin bai motsa ba.",
        "locked": "An kulle biyan. Yatsanka ne kawai zai iya budewa.",
        "unlock_prompt": "Sanya yatsanka a na'urar don bude biyan.",
        "error_generic": "Wani abu ya faru. Kudin bai motsa ba.",
        "error_network": "Ba zan iya isa banki ba. Kudin bai motsa ba.",
        "error_stt": "Ba zan iya sarrafa magana ba. Kudin bai motsa ba.",
        "error_tts": "Ba zan iya fadin karatun ba. Kudin bai motsa ba.",
        "error_bank": "Banki ba ya samuwa. Kudin bai motsa ba.",
        "error_timeout": "Ya dauki lokaci mai tsawo. Kudin bai motsa ba. Sake gwadawa.",
        "history_summary": "Kana da ma'amaloli {count} a {period}. {summary}",
        "history_empty": "Ban sami ma'amala ba a {period}.",
        "receipt": "Rasit na {amount_display} zuwa {recipient}. Matsayi {status}. Lambar {reference}.",
        "help": "Zan iya aika kudi da murya. Ka ce, aika dubu biyar naira zuwa GTBank asusu zero one two three four five six seven eight nine. Murya tana shirya kawai. Yatsanka ne ke amincewa.",
        "consent_required": "Don Allah ka ba da izini kafin mu fara.",
        "device_required": "Ba a yi rijistar wannan na'urar ba. Don Allah yi rijistar yatsanka.",
        "transfer_locked": "An kulle biyan. Kudin bai motsa ba.",
        "too_many_attempts": "Amsoshi da yawa da ban gane ba. Na soke wannan shiri. Kudin bai motsa ba.",
        "approval_required": "Murya ba za ta iya motsa kudi ba. Yi amfani da yatsanka.",
        "unsupported_language": "Wannan harshe bai ci jarrabawar mu ba tukuna. Zan ci gaba da Turanci.",
    },
    "ig": {
        "listening": "Ana m ntị. Kwuo ihe ị chọrọ ime.",
        "listening_resume": "Ana m ntị ọzọ.",
        "session_ready": "Avenza dị njikere. Kwuo arịrịọ ịkwụ ụgwọ.",
        "ask_amount": "Ego ole ka ị chọrọ iziga?",
        "ask_account": "Kedu nọmba akaụntụ m ga-eziga ya?",
        "ask_bank": "Kedu ụlọ akụ akaụntụ ahụ dị?",
        "ask_all": "Kwuo ego, nọmba akaụntụ, na ụlọ akụ.",
        "clarify_amount": "Anụghị m otu ego doro anya. Biko kwuo ego ahụ ọzọ. Ego akwaghị.",
        "clarify_account": "Anụghị m otu nọmba akaụntụ nwere ọnụọgụ iri. Biko kwuo ya ọzọ. Ego akwaghị.",
        "no_speech": "Anụghị m gị. Ego akwaghị.",
        "unclear": "Aghọtaghị m. Ego akwaghị.",
        "readback": "Ị na-achọ iziga {amount_words}, ya bụ {amount_display}, nye {recipient}, akaụntụ na-ejedebe na {last4}, {bank}. Kwuo ee ka ịga n'ihu, mba ka ịkagbu, ma ọ bụ kwuo ọzọ.",
        "duplicate_warning": "Nke a yiri ịkwụ ụgwọ i mere n'oge na-adịbeghị anya n'otu akaụntụ na otu ego.",
        "new_recipient_warning": "Ọ bụ onye nnata ọhụrụ na ego buru ibu. Biko lee ya anya.",
        "name_mismatch_warning": "Aha ụlọ akụ kwadoro bụ {recipient}. Anụrụ m {hint}. Aha ụlọ akụ ka m ga-eji.",
        "confirm_voice": "Daalụ. Tinye mkpịsị aka gị na ngwaọrụ iji kwado ịkwụ ụgwọ a.",
        "fingerprint_retry": "Agụghị m mkpịsị aka gị. Nwaa ọzọ, ma ọ bụ kwuo kagbuo.",
        "approval_expired": "Oge nkwado agwụla. Ego akwaghị.",
        "approved": "Mkpịsị aka kwadoro. Ana eziga na ụlọ akụ.",
        "settled": "Ịkwụ ụgwọ kwụrụ. {amount_display} nye {recipient}. Nọmba {reference}.",
        "failed": "Ịkwụ ụgwọ dara. Ego akwaghị. {reason}",
        "pending": "Ụlọ akụ akwadobeghị. M ga-elele. Ezigala ọzọ.",
        "cancelled": "A kagburu. Ego akwaghị.",
        "expired": "Oge agwụla. Ego akwaghị.",
        "locked": "Akpọchiri ịkwụ ụgwọ. Naanị mkpịsị aka gị nwere ike imeghe ya.",
        "unlock_prompt": "Tinye mkpịsị aka gị na ngwaọrụ iji mepee ịkwụ ụgwọ.",
        "error_generic": "Ihe mere. Ego akwaghị.",
        "error_network": "Enweghị m ike iru ụlọ akụ. Ego akwaghị.",
        "error_stt": "Enweghị m ike ịhazi okwu. Ego akwaghị.",
        "error_tts": "Enweghị m ike ịkpọ ọgụgụ ahụ. Ego akwaghị.",
        "error_bank": "Ụlọ akụ adịghị. Ego akwaghị.",
        "error_timeout": "O were oge. Ego akwaghị. Nwaa ọzọ.",
        "history_summary": "Ị nwere azụmahịa {count} na {period}. {summary}",
        "history_empty": "Ahụghị m azụmahịa na {period}.",
        "receipt": "Rasịtị maka {amount_display} nye {recipient}. Ọnọdụ {status}. Nọmba {reference}.",
        "help": "Enwere m ike iziga ego n'olu. Kwuo, ziga puku ise naira na GTBank akaụntụ zero one two three four five six seven eight nine. Olu na-ede naanị. Mkpịsị aka gị na-akwado.",
        "consent_required": "Biko nye nkwenye tupu anyị amalite.",
        "device_required": "Edebanyeghị ngwaọrụ a. Biko debanye mkpịsị aka gị.",
        "transfer_locked": "Akpọchiri ịkwụ ụgwọ. Ego akwaghị.",
        "too_many_attempts": "Azịza ọtụtụ aghọtaghị m. Akagburu m akwụkwọ a. Ego akwaghị.",
        "approval_required": "Olu enweghị ike ibugharị ego. Jiri mkpịsị aka gị.",
        "unsupported_language": "Asụsụ a agafeghị ule anyị. M ga-aga n'ihu na Bekee.",
    },
    "pcm": {
        "listening": "I dey listen. Talk wetin you want do.",
        "listening_resume": "I dey listen again.",
        "session_ready": "Avenza don ready. Talk your payment request.",
        "ask_amount": "How much you want send?",
        "ask_account": "Which account number I go send am to?",
        "ask_bank": "Which bank that account dey?",
        "ask_all": "Talk the amount, account number, and bank.",
        "clarify_amount": "I no hear one clear amount. Abeg talk the amount again. Money no move.",
        "clarify_account": "I no hear one clear ten digit account number. Abeg talk am again. Money no move.",
        "no_speech": "I no hear you. Money no move.",
        "unclear": "I no understand. Money no move.",
        "readback": "You wan send {amount_words}, that is {amount_display}, to {recipient}, account wey end with {last4}, {bank}. Talk yes to continue, no to cancel, or repeat.",
        "duplicate_warning": "This one look like payment you make recently to the same account for the same amount.",
        "new_recipient_warning": "This na new person and the amount big. Abeg check am well.",
        "name_mismatch_warning": "The name wey bank verify na {recipient}. I hear {hint}. Na the bank name I go use.",
        "confirm_voice": "Thank you. Put your finger on the sensor to approve this exact payment.",
        "fingerprint_retry": "I no fit read your finger. Try again, or talk cancel.",
        "approval_expired": "The approval don expire. Money no move.",
        "approved": "Fingerprint approve. I dey send to bank.",
        "settled": "Payment settle. {amount_display} to {recipient}. Reference {reference}.",
        "failed": "Payment fail. Money no move. {reason}",
        "pending": "Bank never confirm. I go check the status. No send again.",
        "cancelled": "I cancel am. Money no move.",
        "expired": "This draft expire. Money no move.",
        "locked": "Transfers lock. Only your finger fit unlock am.",
        "unlock_prompt": "Put your finger on the sensor to unlock transfers.",
        "error_generic": "Something happen. Money no move.",
        "error_network": "I no fit reach bank. Money no move.",
        "error_stt": "I no fit process speech. Money no move.",
        "error_tts": "I no fit talk the read-back. Money no move.",
        "error_bank": "Bank no dey. Money no move.",
        "error_timeout": "E take too long. Money no move. Try again.",
        "history_summary": "You get {count} transactions in {period}. {summary}",
        "history_empty": "I no find any transaction in {period}.",
        "receipt": "Receipt for {amount_display} to {recipient}. Status {status}. Reference {reference}.",
        "help": "I fit send money with voice. Talk, send five thousand naira to GTBank account zero one two three four five six seven eight nine. Voice only draft. Your finger dey approve.",
        "consent_required": "Abeg record your consent before we start.",
        "device_required": "This device no dey enrolled. Abeg enrol your fingerprint.",
        "transfer_locked": "Transfers lock. Money no move.",
        "too_many_attempts": "Too many replies I no understand. I cancel this draft. Money no move.",
        "approval_required": "Voice no fit move money. Use your finger.",
        "unsupported_language": "This language never pass our test. I go continue in English.",
    },
}


@dataclass(frozen=True)
class Prompt:
    id: str
    text: str
    language: str
    audio_url: str | None


class PromptLibrary:
    def __init__(self, prompt_dir: Path | None = None):
        self.prompt_dir = prompt_dir

    def render(self, prompt_id: str, language: str = "en", **kwargs) -> Prompt:
        lang = language if language in PROMPTS else "en"
        table = PROMPTS[lang]
        if prompt_id not in table:
            table = PROMPTS["en"]
            lang = "en"
        text = table[prompt_id]
        values = defaultdict(str, **kwargs)
        try:
            text = text.format_map(values)
        except (KeyError, ValueError):
            pass
        audio_url = None
        if self.prompt_dir:
            candidate = self.prompt_dir / f"{lang}-{prompt_id}.wav"
            fallback = self.prompt_dir / f"en-{prompt_id}.wav"
            if candidate.exists():
                audio_url = f"/prompts/{candidate.name}"
            elif fallback.exists():
                audio_url = f"/prompts/{fallback.name}"
        return Prompt(id=prompt_id, text=text, language=lang, audio_url=audio_url)

    def all_ids(self) -> list[str]:
        return sorted(PROMPTS["en"].keys())

    @staticmethod
    def language_supported(language: str) -> bool:
        return language in PROMPTS

    @staticmethod
    def verified_languages() -> list[str]:
        # Only English has been checked by a native speaker in this build.
        return ["en"]
