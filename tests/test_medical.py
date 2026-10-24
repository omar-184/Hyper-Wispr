import copy
import json
import time

import pytest

from hyper_wispr import ai_cleanup, config, context, hotkeys, medical
from hyper_wispr.app import process_text


def settings(**medical_cfg):
    s = copy.deepcopy(config.DEFAULTS)
    s["ai_cleanup"]["enabled"] = False
    s["medical"].update(medical_cfg)
    return s


@pytest.fixture(autouse=True)
def no_user_lists(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)
    return tmp_path


# ---- the dictionary -------------------------------------------------------------

def test_every_pack_loads_with_a_title_and_terms():
    packs = medical.builtin_packs()
    for pack_id in medical.SPECIALTIES:
        assert packs[pack_id].kind == "specialty"
    for pack in packs.values():
        assert pack.title and pack.terms, pack.id
        assert not pack.title.startswith(("core", "specialty", "fda")), pack.id
    assert len(packs["fda-generics"].terms) > 1000 and len(packs["fda-brands"].terms) > 1000


def test_core_words_are_spelled_like_the_fda_spells_them():
    generics = {t.lower() for t in medical.builtin_packs()["fda-generics"].terms}
    for drug in ("metoprolol", "apixaban", "levetiracetam", "ceftriaxone", "nimodipine", "dexmedetomidine"):
        assert drug in generics


def test_specialties_and_fda_list_can_be_switched_off():
    ids = lambda s: {p.id for p in medical.active_packs(s)}  # noqa: E731
    assert "neuro" not in ids(settings()) and "neuro" in ids(settings(specialties=["neuro"]))
    assert "fda-generics" not in ids(settings(fda_drug_names=False))


def test_hidden_and_custom_words():
    s = settings(custom_terms=["Farghal"], hidden_terms=["Lasix"])
    lex = medical.lexicon(s)
    assert lex.terms["farghal"] == "Farghal" and "lasix" not in lex.terms


def test_own_word_lists_from_the_folder(no_user_lists):
    folder = no_user_lists / "medical"
    folder.mkdir()
    (folder / "ward.txt").write_text("﻿# Title: ignored for own lists\n*Hyperwisprazole\nKocher maneuver\n", encoding="utf-8")
    (folder / "README.txt").write_text("not a word list\n", encoding="utf-8")
    s = settings()
    assert [p.title for p in medical.user_packs()] == ["ward"]
    assert "hyperwisprazole" in medical.lexicon(s).terms
    assert "Hyperwisprazole" in medical.speech_prompt(s)


# ---- spelling fixes ----------------------------------------------------------------

@pytest.mark.parametrize("heard, written", [
    ("start metoprolal 25 mg", "start metoprolol 25 mg"),
    ("give levo floxacin 750 mg IV", "give levofloxacin 750 mg IV"),
    ("on apixiban for atrial fibrilation", "on apixaban for atrial fibrillation"),
    ("cef triaxone 2 g daily", "ceftriaxone 2 g daily"),
    ("started dexmedetomadine", "started dexmedetomidine"),
])
def test_near_misses_are_corrected(heard, written):
    assert medical.fix(heard, settings(format_units=False)) == written


def test_specialty_words_are_corrected_when_the_specialty_is_on():
    assert medical.fix("plan for craniotamy", settings(specialties=["neuro"])) == "plan for craniotomy"


def test_ordinary_text_is_left_alone():
    text = ("Thanks for meeting with me yesterday. The committee discussed the proposal, and everyone agreed "
            "that the schedule works. Please send the presentation and remember the parking situation downtown. "
            "Dr. Farghal will call the patient's daughter tomorrow afternoon about the appointment.")
    assert medical.fix(text, settings()) == text


def test_your_dictionary_words_are_never_corrected():
    s = settings()
    s["dictionary"] = ["Metoprolax"]
    assert medical.fix("Metoprolax was there", s) == "Metoprolax was there"


def test_nothing_changes_when_medical_dictation_is_off():
    text = "start metoprolal 25 milligrams PO b.i.d."
    assert medical.fix(text, settings(enabled=False)) == text


def test_switches_for_each_fix():
    assert medical.fix("metoprolal 25 milligrams", settings(fix_spelling=False)) == "metoprolal 25 mg"
    assert medical.fix("metoprolal 25 milligrams", settings(format_units=False)) == "metoprolol 25 milligrams"


# ---- units and abbreviations --------------------------------------------------------

@pytest.mark.parametrize("heard, written", [
    ("25 milligrams PO", "25 mg PO"),
    ("0.4 micrograms per kilogram per hour", "0.4 mcg/kg/h"),
    ("2 liters per minute", "2 L/min"),
    ("100 milliliters per hour", "100 mL/h"),
    ("20 milliequivalents of potassium", "20 mEq of potassium"),
    ("BP 120 over 80", "BP 120/80"),
    ("blood pressure was 135 over 85", "blood pressure was 135/85"),
    ("140 over 90 mmHg", "140/90 mmHg"),
    ("heart rate 88 beats per minute", "heart rate 88 bpm"),
    ("sats 95 percent", "sats 95%"),
    ("temperature 38.5 degrees Celsius", "temperature 38.5°C"),
    ("give 1 gram", "give 1 g"),
])
def test_units_are_written_the_standard_way(heard, written):
    assert medical.format_units(heard) == written


@pytest.mark.parametrize("text", [
    "for 2 hours", "every 4 hours", "wait 10 minutes", "a few milligrams", "100 over 50 years", "grams of protein",
])
def test_durations_and_ordinary_phrases_keep_their_words(text):
    assert medical.format_units(text) == text


def test_dotted_abbreviations():
    assert medical.fix("metformin 500 mg p.o. b.i.d. and p.r.n.", settings()) == "metformin 500 mg PO BID and PRN."


def test_fixes_never_change_a_number():
    samples = [
        "give metoprolal 12.5 milligrams b.i.d., BP 140 over 90, sats 92 percent on 3 liters per minute",
        "heparin 18 units per kilogram per hour, potassium 3.2, creatinine 1.4, 5 micrograms per kilogram per minute",
        "G2P1 at 34 weeks, 2 grams of magnesium over 20 minutes",
    ]
    for text in samples:
        assert ai_cleanup.numbers_kept(text, medical.fix(text, settings(specialties=list(medical.SPECIALTIES))), exact=True)


def test_fixing_is_fast():
    text = " ".join(["The patient started metoprolal 25 milligrams and apixiban, BP 130 over 70."] * 30)
    s = settings(specialties=list(medical.SPECIALTIES))
    medical.fix("warm up", s)
    start = time.perf_counter()
    medical.fix(text, s)
    assert time.perf_counter() - start < 1.0


# ---- the whole dictation --------------------------------------------------------------

def test_process_text_applies_the_medical_fixes():
    out = process_text("start metoprolal 25 milligrams p.o. b.i.d. period", settings(), context.DOCUMENT)
    assert out == "Start metoprolol 25 mg PO BID."


# ---- the speech model's hint -----------------------------------------------------------

def test_speech_prompt_fits_and_keeps_the_important_words():
    s = settings(specialties=["neuro"], custom_terms=["Farghal"])
    s["dictionary"] = [f"word{i}" for i in range(200)]
    s["dictionary_starred"] = ["Omar"]
    prompt = medical.speech_prompt(s)
    assert prompt.startswith("Medical dictation. Vocabulary: ")
    assert len(prompt) // 3 + 1 <= medical.PROMPT_TOKENS
    assert prompt.rstrip(".").endswith("Omar") and "Farghal" in prompt


def test_speech_prompt_puts_specialty_terms_before_core_ones():
    prompt = medical.speech_prompt(settings(specialties=["neuro"]))
    assert "craniotomy" in prompt and "nimodipine" in prompt


def test_speech_prompt_without_medical_dictation():
    s = settings(enabled=False)
    assert medical.speech_prompt(s) is None
    s["dictionary"] = ["Farghal"]
    assert medical.speech_prompt(s) == "Vocabulary: Farghal."


def test_speech_prompt_uses_the_real_token_count():
    prompt = medical.speech_prompt(settings(specialties=list(medical.SPECIALTIES)), count_tokens=lambda t: len(t.split()))
    assert len(prompt.split()) <= medical.PROMPT_TOKENS


# ---- the AI step ---------------------------------------------------------------------

def test_ai_gets_the_terms_that_sound_like_the_dictation():
    s = settings()
    s["ai_cleanup"]["enabled"] = True
    prompt = ai_cleanup.build_system_prompt(s, context.DOCUMENT, text="start metaprolol and keprah tomorrow")
    assert "Medical writing rules" in prompt and "metoprolol" in prompt and "Keppra" in prompt
    assert "tomorrow" not in prompt.split("never add one that wasn't said:")[1]


def test_ai_has_no_medical_rules_when_off():
    s = settings(enabled=False)
    assert "Medical" not in ai_cleanup.build_system_prompt(s, context.DOCUMENT, text="start metoprolol")


def test_ai_terms_ignore_everyday_words():
    assert medical.ai_terms("thanks for the lovely dinner yesterday", settings()) == []


def test_medical_dictation_keeps_every_number_in_the_ai_step(monkeypatch):
    s = settings()
    s["ai_cleanup"]["enabled"] = True
    monkeypatch.setattr(ai_cleanup, "_post", lambda *a, **k: {"message": {"content": "At 6 mg."}})
    assert ai_cleanup.refine("at 5 mg no at 6 mg", s, context.OTHER) == "at 5 mg no at 6 mg"
    s["medical"]["enabled"] = False
    assert ai_cleanup.refine("at 5 mg no at 6 mg", s, context.OTHER) == "At 6 mg."


# ---- settings ------------------------------------------------------------------------

def test_old_medical_mode_setting_is_carried_over(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"ai_cleanup": {"medical_mode": False, "model": "qwen2.5:7b"}}), encoding="utf-8")
    s = config.load(p)
    assert s["medical"]["enabled"] is False and "medical_mode" not in s["ai_cleanup"]
    assert s["ai_cleanup"]["model"] == "qwen2.5:7b" and s["medical"]["format_units"] is True
    p.write_text(json.dumps({"ai_cleanup": {"medical_mode": True}}), encoding="utf-8")
    assert config.load(p)["medical"]["enabled"] is True


def test_new_settings_files_have_medical_dictation_on(tmp_path):
    s = config.load(tmp_path / "settings.json")
    assert s["medical"]["enabled"] is True and s["medical"]["specialties"] == []


# ---- the on/off shortcut ---------------------------------------------------------------

def test_medical_shortcut_toggles_when_idle_only():
    calls = []
    m = hotkeys.HotkeyStateMachine(["ctrl_l", "cmd"], 0.35, lambda: calls.append("start"), lambda: calls.append("stop"),
                                   lambda: None, medical_keys=["ctrl_l", "alt_l", "m"], on_medical=lambda: calls.append("medical"))
    for k in ("ctrl_l", "alt_l", "m"):
        m.key_down(k)
    for k in ("m", "alt_l", "ctrl_l"):
        m.key_up(k)
    assert calls == ["medical"]
    m.key_down("ctrl_l")
    m.key_down("alt_l")
    m.key_down("m")  # auto-repeat of a held key doesn't toggle again
    m.key_down("m")
    assert calls == ["medical", "medical"]


# ---- the window -------------------------------------------------------------------------

def test_window_api_for_the_medical_page():
    from hyper_wispr.settings_ui import Api

    s = settings(specialties=["neuro"])
    info = Api().medical_info(s)
    assert info["ok"] and info["total"] > 4000
    neuro = next(p for p in info["packs"] if p["id"] == "neuro")
    assert neuro["on"] and neuro["count"] > 100
    found = Api().medical_lookup("levetir", s)
    assert found[0]["term"] == "levetiracetam"
    s["medical"]["hidden_terms"] = ["levetiracetam"]
    assert any(f["hidden"] for f in Api().medical_lookup("levetir", s))
    assert Api().medical_try("start metoprolal 25 milligrams", settings(enabled=False)) == "Start metoprolol 25 mg"
