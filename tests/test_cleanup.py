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
