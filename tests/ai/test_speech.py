import pytest

from avenza_ai.speech import (PROMPT_IDS, ReadbackError, digits_to_words, get_prompts, has_teen_ty_risk,
                              naira_to_words, number_to_words, render_failed, render_readback,
                              render_settled, speakable_name)
from avenza_ai.speech.prompts import CATALOG


def test_number_words():
    assert number_to_words(0) == "zero"
    assert number_to_words(15) == "fifteen" and number_to_words(50) == "fifty"
    assert number_to_words(15000) == "fifteen thousand"
    assert number_to_words(1_000_005) == "one million and five"
    assert number_to_words(250) == "two hundred and fifty"
    assert number_to_words(120_000_000) == "one hundred and twenty million"
    with pytest.raises(ValueError):
        number_to_words(-1)


def test_naira_words():
    assert naira_to_words(1500000) == "fifteen thousand naira"
    assert naira_to_words(100) == "one naira"
    assert naira_to_words(150050) == "one thousand five hundred naira and fifty kobo"
    assert naira_to_words(50) == "fifty kobo"
    for bad in (0, -5, 1.5):
        with pytest.raises(ValueError):
            naira_to_words(bad)


def test_digit_hint_and_risk():
    assert digits_to_words(15000) == "one five, zero zero zero"
    assert digits_to_words(50) == "five zero"
    for n in (15, 50, 15000, 50000, 18000, 80000, 1_015_000):
        assert has_teen_ty_risk(n), n
    for n in (5, 20, 5000, 12500, 1_200_000, 100):
        assert not has_teen_ty_risk(n), n


def test_readback_reads_full_verified_name_and_amount():
    r = render_readback(1500000, "AISHA BELLO-OKAFOR", bank_name="GTBank")
    assert "fifteen thousand naira" in r.text and "Aisha Bello-Okafor" in r.text and "at GTBank" in r.text
    assert r.spelled_digits and "one five, zero zero zero naira" in r.text
    assert r.text.endswith("Say yes, no, or repeat.")


def test_similar_names_stay_distinct():
    a = render_readback(500000, "Aisha Bello").text
    b = render_readback(500000, "Aisha Bello-Okafor").text
    assert a != b and "Okafor" in b and "Okafor" not in a


def test_no_digit_hint_when_not_confusable():
    r = render_readback(500000, "Musa Ibrahim")
    assert not r.spelled_digits and "That is" not in r.text


def test_pidgin_readback():
    r = render_readback(500000, "Musa Ibrahim", language="pcm")
    assert r.language_used == "pcm" and r.text.startswith("You wan send five thousand naira to Musa Ibrahim")


def test_unreviewed_language_falls_back_to_english():
    assert render_readback(500000, "Musa Ibrahim", language="yo").language_used == "en"
    assert get_prompts("ha")[1] == "en"


@pytest.mark.parametrize("name", [None, "", "   ", "1234", "!!!"])
def test_missing_or_junk_name_is_refused(name):
    with pytest.raises(ReadbackError):
        render_readback(500000, name)


@pytest.mark.parametrize("kobo", [0, -1, 12.5, None])
def test_bad_amount_is_refused(kobo):
    with pytest.raises(ReadbackError):
        render_readback(kobo, "Musa Ibrahim")


def test_speakable_name():
    assert speakable_name("  ADEBAYO   OGUNLEYE ") == "Adebayo Ogunleye"
    assert speakable_name("Chinedu O'Brien") == "Chinedu O'Brien"
    assert speakable_name("NGOZI EZE (SAVINGS)") == "Ngozi Eze Savings"


def test_settled_and_failed():
    assert "fifteen thousand naira has been sent to Aisha Bello" in render_settled(1500000, "AISHA BELLO")
    assert "did not go through" in render_failed("en")


def test_every_language_has_every_prompt():
    for lang, prompts in CATALOG.items():
        assert set(prompts) == set(PROMPT_IDS), lang


def test_money_not_moved_claims_are_only_on_pre_submission_prompts():
    for lang, prompts in CATALOG.items():
        for p in prompts.values():
            says = "No money has moved" in p.text or "Money never comot" in p.text
            assert says == p.claims_no_money_moved, (lang, p.id)
            if p.claims_no_money_moved:
                assert "before the transfer is submitted" in p.truthful_when, p.id
    assert not CATALOG["en"]["error_status_unknown"].claims_no_money_moved
    assert not CATALOG["en"]["result_failed"].claims_no_money_moved
