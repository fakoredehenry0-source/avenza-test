import pytest

from avenza_ai.parsing import AmountStatus, parse_account_number, parse_amount
from avenza_ai.speech.numwords import number_to_words

ALL = ("yo", "ha", "ig")


def kobo(text, **kw):
    r = parse_amount(text, **kw)
    assert r.ok, (text, r)
    return r.kobo


# ---- the doc's named case: fifteen versus fifty --------------------------------------
@pytest.mark.parametrize("text,naira", [
    ("fifteen", 15), ("fifty", 50), ("fifteen naira", 15), ("fifty naira", 50),
    ("fifteen thousand naira", 15000), ("fifty thousand naira", 50000),
    ("fifteen hundred naira", 1500), ("five hundred naira", 500),
    ("eighteen thousand", 18000), ("eighty thousand", 80000),
    ("thirteen thousand", 13000), ("thirty thousand", 30000),
    ("1,500 naira", 1500), ("1500 naira", 1500), ("15,000", 15000), ("50,000", 50000),
])
def test_fifteen_versus_fifty(text, naira):
    assert kobo("send " + text + " to Aisha") == naira * 100


@pytest.mark.parametrize("text", ["fifteen fifty", "fifty fifteen", "fifteen fifteen", "fifty fifty"])
def test_run_together_teens_and_tens_are_rejected(text):
    r = parse_amount(text)
    assert r.status is AmountStatus.AMBIGUOUS


# ---- spoken English -------------------------------------------------------------------
@pytest.mark.parametrize("text,naira", [
    ("five thousand", 5000), ("a thousand naira", 1000), ("a hundred naira", 100),
    ("two hundred and fifty naira", 250), ("two hundred fifty", 250), ("one hundred and five", 105),
    ("twenty five hundred", 2500), ("one thousand and five", 1005), ("one thousand five hundred", 1500),
    ("twelve thousand five hundred", 12500), ("two hundred and fifty thousand", 250000),
    ("five hundred thousand naira", 500000), ("one million two hundred thousand", 1_200_000),
    ("two million", 2_000_000), ("twenty one", 21), ("ninety nine thousand nine hundred and ninety nine", 99999),
])
def test_spoken_english(text, naira):
    assert kobo(text) == naira * 100


@pytest.mark.parametrize("text,naira", [
    ("5k", 5000), ("5 k", 5000), ("2.5k naira", 2500), ("5 thousand", 5000), ("2.5 million", 2_500_000),
    ("5 hundred", 500), ("₦5,000", 5000), ("N5000", 5000), ("NGN 7,500", 7500),
    ("7500", 7500), ("1,000,000", 1_000_000), ("1,500.50", 1500.5),
])
def test_digits_and_shorthand(text, naira):
    assert kobo("send " + text) == int(round(naira * 100))


def test_pidgin_uses_english_numbers():
    assert kobo("abeg send am fifty thousand naira give Musa") == 5_000_000
    assert kobo("abeg send five k give me") == 500_000


# ---- ambiguity is rejected, never guessed --------------------------------------------
@pytest.mark.parametrize("text,reason", [
    ("send five thousand and ten thousand", "invalid_number_grammar"),
    ("send five thousand ten thousand naira", "invalid_number_grammar"),
    ("send thousand naira", "invalid_number_grammar"),
    ("send five thousand naira fifty kobo", "kobo_not_supported_by_voice"),
    ("send 1,50 naira", "invalid_number_format"),
    ("send 1.555 naira", "fractional_kobo"),
    ("send 5 and 3", "mixed_digits_and_words"),
])
def test_rejected_inputs(text, reason):
    r = parse_amount(text)
    assert not r.ok and r.reason == reason, r


def test_two_different_amounts_are_ambiguous():
    r = parse_amount("send 5000 and then 7000")
    assert r.status is AmountStatus.AMBIGUOUS and r.reason == "multiple_amounts"
    assert set(r.candidates_kobo) == {500000, 700000}


def test_same_amount_twice_is_still_ambiguous():
    assert parse_amount("send 5000 naira, 5000 naira").status is AmountStatus.AMBIGUOUS


def test_currency_marker_breaks_the_tie():
    assert kobo("send 5000 naira to account 12") == 500000


def test_no_amount_and_empty():
    assert parse_amount("send money to Aisha").status is AmountStatus.NONE
    assert parse_amount("").status is AmountStatus.NONE
    assert parse_amount("   ").status is AmountStatus.NONE


def test_range_limits():
    assert parse_amount("send 0 naira").status is AmountStatus.OUT_OF_RANGE
    assert parse_amount("send five hundred million naira").reason == "above_maximum"
    assert parse_amount("send 5000", max_kobo=100_000).reason == "above_maximum"
    assert parse_amount("send 50 naira", min_kobo=10_000).reason == "below_minimum"


def test_pronoun_one_is_not_an_amount():
    assert kobo("send five thousand naira to the one at GTBank") == 500000


# ---- account numbers are never amounts ------------------------------------------------
def test_account_number_digits_are_not_an_amount():
    r = parse_amount("send 5000 naira to 0123456789")
    assert r.ok and r.kobo == 500000


def test_spoken_account_number_is_not_an_amount():
    r = parse_amount("send three thousand naira to account zero one two three four five six seven eight nine")
    assert r.ok and r.kobo == 300000


def test_grouped_account_number_is_not_an_amount():
    assert kobo("send 2000 naira to 012 345 6789") == 200000


def test_account_number_extraction():
    assert parse_account_number("account 0123456789 please") == "0123456789"
    assert parse_account_number("zero one two three four five six seven eight nine") == "0123456789"
    assert parse_account_number("send 5000 naira") is None
    assert parse_account_number("account 12345 or 0123456789 or 9876543210") is None


# ---- Yoruba / Hausa / Igbo are opt-in -------------------------------------------------
def test_local_numbers_are_off_unless_enabled():
    assert parse_amount("dubu biyar naira").status is AmountStatus.NONE
    assert kobo("dubu biyar naira", languages=("ha",)) == 500000


@pytest.mark.parametrize("text,naira,lang", [
    ("Aika naira dubu biyar zuwa Aisha", 5000, "ha"),
    ("dubu goma sha biyar", 15000, "ha"),
    ("dubu hamsin", 50000, "ha"),
    ("dari biyu da hamsin", 250, "ha"),
    ("dubu ashirin da biyar", 25000, "ha"),
    ("goma sha biyar", 15, "ha"),
    ("Zipu naira puku ise nye Chinedu", 5000, "ig"),
    ("puku iri", 10000, "ig"),
    ("puku iri abuo", 20000, "ig"),
    ("nari abuo na iri ise", 250, "ig"),
    ("iri na ise", 15, "ig"),
    ("Fi naira ẹgbẹ̀rún márùn-ún ránṣẹ́ sí Aisha", 5000, "yo"),
    ("ẹgbẹ̀rún mẹ́wàá", 10000, "yo"),
    ("ẹgbẹ̀rún aadọ́ta", 50000, "yo"),
    ("ọgọ́rùn-ún", 100, "yo"),
])
def test_local_languages_when_enabled(text, naira, lang):
    r = parse_amount(text, languages=(lang,))
    assert r.ok and r.kobo == naira * 100 and r.needs_review, r


@pytest.mark.parametrize("text,naira", [
    ("Aika five thousand naira zuwa Aisha", 5000),
    ("Zipu naira puku twelve nye Ada", 12000),
    ("dubu twelve naira", 12000),
    ("egberun fifteen naira", 15000),
    ("puku 5 naira", 5000),
])
def test_mixed_language_numbers(text, naira):
    assert kobo(text, languages=ALL) == naira * 100


@pytest.mark.parametrize("text", [
    "puku iri na ise",          # Igbo compound we refuse to guess (10,005 or 15,000)
    "dubu biyar hamsin",        # missing connector
    "dubu biyar dari biyu",     # missing connector
    "ogorunun marunun",           # Yoruba 500 is irregular: rejected
])
def test_unsure_local_forms_are_rejected(text):
    assert not parse_amount(text, languages=ALL).ok


def test_lone_local_one_is_not_an_amount():
    assert kobo("Fi naira ẹgbẹ̀rún márùn-ún owo kan", languages=("yo",)) == 500000


# ---- round trip with number_to_words ----------------------------------------------------
def test_round_trip_words_and_back():
    import random
    rng = random.Random(1)
    nums = list(range(1, 2001)) + [rng.randint(2001, 9_999_999) for _ in range(500)]
    for n in nums:
        assert kobo(number_to_words(n), max_kobo=10**13) == n * 100, (n, number_to_words(n))


def test_word_number_followed_by_k_means_thousands():
    assert kobo("send twenty five k") == 2_500_000


def test_marked_amount_does_not_hide_a_second_real_amount():
    assert parse_amount("send 5000 or 7000 naira").status is AmountStatus.AMBIGUOUS
    assert parse_amount("send 5000 naira to 12").ok
