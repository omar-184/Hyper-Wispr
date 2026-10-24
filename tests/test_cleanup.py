from hyper_wispr import ai_cleanup, cleanup, config, context
from hyper_wispr.app import process_text


def settings(**overrides):
    s = config.merge(config.DEFAULTS, overrides)
    s["ai_cleanup"]["enabled"] = False
    return s


def test_removes_fillers():
    assert cleanup.clean("um so uh the patient is stable", settings()) == "So the patient is stable"


def test_spoken_punctuation():
    out = cleanup.clean("hello comma how are you question mark new line thanks period", settings())
    assert out == "Hello, how are you?\nThanks."


def test_brackets():
    out = cleanup.clean("see open bracket figure two close bracket", settings())
    assert out == "See (figure two)"


def test_backtrack_removes_previous_clause():
    out = cleanup.clean("Start metformin. Give 10 mg scratch that give 5 mg", settings())
    assert out == "Start metformin. Give 5 mg"


def test_replacements_are_case_insensitive_whole_words():
    s = settings(replacements={"hyper whisper": "Hyper-Wispr"})
    assert cleanup.clean("I love Hyper Whisper", s) == "I love Hyper-Wispr"


def test_snippet_bypasses_cleanup():
    s = settings(snippets={"my address": "1 Main St\nRiyadh"})
    assert process_text("My address.", s, context.OTHER) == "1 Main St\nRiyadh"


def test_ai_skipped_when_disabled():
    assert process_text("this is a test of the system", settings(), context.EMAIL) == "This is a test of the system"


def test_ai_output_rejected_when_model_rambles(monkeypatch):
    s = config.merge(config.DEFAULTS, {})
    monkeypatch.setattr(ai_cleanup, "_post", lambda *a, **k: {"message": {"content": "Sure! " * 100}})
    assert ai_cleanup.refine("what is the dose of amoxicillin", s, context.OTHER) == "what is the dose of amoxicillin"


def test_ai_output_used(monkeypatch):
    s = config.merge(config.DEFAULTS, {})
    monkeypatch.setattr(ai_cleanup, "_post", lambda *a, **k: {"message": {"content": "<dictation>\nGive 5 mg PO BID.\n</dictation>"}})
    assert ai_cleanup.refine("give five mg po bid", s, context.OTHER) == "Give 5 mg PO BID."


def test_system_prompt_includes_dictionary_and_style():
    s = config.merge(config.DEFAULTS, {"dictionary": ["apixaban", "Smith"]})
    prompt = ai_cleanup.build_system_prompt(s, context.EMAIL)
    assert "apixaban, Smith" in prompt and "email" in prompt and "Medical" in prompt


def test_classify_app():
    assert context.classify("OUTLOOK.EXE", "") == context.EMAIL
    assert context.classify("chrome.exe", "Inbox - Gmail") == context.EMAIL
    assert context.classify("chrome.exe", "Some page") == context.OTHER


def test_settings_merge_keeps_new_defaults(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('{"speech": {"model": "medium"}}', encoding="utf-8")
    s = config.load(p)
    assert s["speech"]["model"] == "medium" and s["speech"]["beam_size"] == 5


# ---- medical dictation must not be altered ------------------------------------

def test_units_and_abbreviations_are_not_fillers():
    s = settings(cleanup={"filler_words": ["um", "er", "mm"]})  # the old list, still in some settings files
    assert cleanup.clean("there is a 5 mm nodule", s) == "There is a 5 mm nodule"
    assert cleanup.clean("blood pressure 120/80 mm Hg", s) == "Blood pressure 120/80 mm Hg"
    assert cleanup.clean("um sent to the ER for evaluation", s) == "Sent to the ER for evaluation"


def test_fillers_between_commas_leave_clean_punctuation():
    assert cleanup.clean("um, uh, the patient, um, is fine", settings()) == "The patient is fine"
    assert cleanup.clean("The patient is stable um. Next issue", settings()) == "The patient is stable. Next issue"


def test_command_words_that_are_also_clinical_words_stay_words():
    for text in (
        "there is a mass in the sigmoid colon",
        "colon cancer screening is due",
        "the postoperative period was uneventful",
        "over a six week period she improved",
        "a new line was placed in the right IJ",
        "we discussed the next line of therapy",
    ):
        assert cleanup.clean(text, settings()).lower() == text.lower()


def test_spoken_commands_still_work_in_notes():
    assert cleanup.clean("Assessment colon stable angina", settings()) == "Assessment: stable angina"
    out = cleanup.clean("the patient is stable period follow up in two weeks period", settings())
    assert out == "The patient is stable. Follow up in two weeks."


def test_backtrack_after_spoken_period_keeps_the_earlier_sentence():
    out = cleanup.clean("Start metformin period give 10 mg scratch that give 5 mg", settings())
    assert out == "Start metformin. Give 5 mg"


def test_replacement_text_is_inserted_literally():
    s = settings(replacements={"my folder": r"C:\Users\Omar", "": "ignored", "  ": "ignored"})
    assert cleanup.clean("saved in my folder today", s) == r"Saved in C:\Users\Omar today"


def test_old_filler_list_is_upgraded(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('{"cleanup": {"filler_words": ["um", "uh", "erm", "er", "ah", "hmm", "mm", "uhm"]}, "hands_free_extra_key": "space"}', encoding="utf-8")
    s = config.load(p)
    assert "er" not in s["cleanup"]["filler_words"] and "mm" not in s["cleanup"]["filler_words"]
    assert "hands_free_extra_key" not in s


def test_settings_saved_by_notepad_with_a_byte_order_mark_load(tmp_path):
    p = tmp_path / "settings.json"
    p.write_bytes(b'\xef\xbb\xbf{"speech": {"model": "small"}}')
    assert config.load(p)["speech"]["model"] == "small"


# ---- the AI step may not change numbers ---------------------------------------

def test_spoken_numbers_match_digits():
    assert ai_cleanup.numbers_kept("give five mg po bid", "Give 5 mg PO BID.", exact=True)
    assert ai_cleanup.numbers_kept("two hundred fifty mg", "250 mg", exact=True)
    assert ai_cleanup.numbers_kept("hemoglobin of twelve point five", "Hemoglobin of 12.5.", exact=True)
    assert ai_cleanup.numbers_kept("1,000 units", "1000 units", exact=True)
    assert ai_cleanup.numbers_kept("الجرعة ٥ ملغ", "الجرعة 5 ملغ", exact=True)


def test_list_numbers_added_by_formatting_are_ignored():
    before = "first take aspirin second take metformin 500 mg"
    assert ai_cleanup.numbers_kept(before, "1. Take aspirin\n2. Take metformin 500 mg", exact=True)


def test_changed_or_added_numbers_are_caught():
    assert not ai_cleanup.numbers_kept("give 5 mg", "Give 50 mg", exact=False)
    assert not ai_cleanup.numbers_kept("give 5 mg", "Give 5 mg twice daily for 7 days", exact=False)
    # A correction may drop the first number, but not in medical mode.
    assert ai_cleanup.numbers_kept("at 5 no at 6", "At 6", exact=False)
    assert not ai_cleanup.numbers_kept("at 5 no at 6", "At 6", exact=True)


def test_ai_output_that_changes_a_dose_is_rejected(monkeypatch):
    s = config.merge(config.DEFAULTS, {})
    monkeypatch.setattr(ai_cleanup, "_post", lambda *a, **k: {"message": {"content": "Give metoprolol 50 mg PO BID."}})
    assert ai_cleanup.refine("give metoprolol 25 mg po bid", s, context.OTHER) == "give metoprolol 25 mg po bid"


def test_ai_failure_of_any_kind_keeps_the_text(monkeypatch):
    def broken(*a, **k):
        raise ConnectionResetError("Ollama restarted")

    monkeypatch.setattr(ai_cleanup, "_post", broken)
    s = config.merge(config.DEFAULTS, {})
    assert ai_cleanup.refine("the patient is doing well today", s, context.OTHER) == "the patient is doing well today"
