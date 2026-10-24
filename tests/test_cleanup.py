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


def test_a_list_said_with_numbers_can_come_back_numbered():
    """In medical mode a plan dictated "number one, ... number two, ..." never got its list: the
    spoken one and two were missing from the answer, so it was thrown away."""
    said = "Plan: number one, start aspirin. Number two, hold heparin 5000 units."
    assert ai_cleanup.numbers_kept(said, "Plan:\n1. Start aspirin.\n2. Hold heparin 5000 units.", exact=True)
    assert not ai_cleanup.numbers_kept(said, "Plan:\n1. Start aspirin.\n2. Hold heparin.", exact=True)
    assert not ai_cleanup.numbers_kept("give 2 tablets now", "1. Give tablets now", exact=True)


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


def test_ai_never_adds_a_route_or_how_often(monkeypatch):
    """Measured with qwen2.5:3b: "apixaban 5 milligrams twice a day" came back "5 mg PO BID"."""
    s = config.merge(config.DEFAULTS, {})
    said = "Start apixaban 5 mg twice a day and furosemide 40 mg intravenously."
    monkeypatch.setattr(ai_cleanup, "_post", lambda *a, **k: {"message": {"content": "Start apixaban 5 mg PO BID. Furosemide 40 mg IV."}})
    assert ai_cleanup.refine(said, s, context.DOCUMENT) == said
    # Writing what was said in short is fine.
    monkeypatch.setattr(ai_cleanup, "_post", lambda *a, **k: {"message": {"content": "Start apixaban 5 mg BID and furosemide 40 mg IV."}})
    assert ai_cleanup.refine(said, s, context.DOCUMENT) == "Start apixaban 5 mg BID and furosemide 40 mg IV."
    assert ai_cleanup.adds_route("give tylenol 1 g", "Give Tylenol 1 g PRN.")
    assert ai_cleanup.adds_route("start heparin", "Start heparin STAT.")
    assert not ai_cleanup.adds_route("give it by mouth as needed at night", "Give it PO PRN QHS.")
    assert not ai_cleanup.adds_route("I'm seeing the PR interval", "I'm seeing the PR interval.")


def test_ai_never_drops_a_negation_or_a_drug(monkeypatch):
    """The number check doesn't see a lost "no" or a swapped drug; measured with qwen2.5:3b, the model
    once left out a sentence of a dictation and wrote one that wasn't said."""
    s = config.merge(config.DEFAULTS, {})
    for said, answer in [
        ("Metoprolol no longer needed. Levetiracetam no change.", "Metoprolol needed. Levetiracetam no change."),
        ("Patient denies chest pain today.", "Patient has chest pain today."),
        ("Start hydralazine 10 mg IV now.", "Start hydroxyzine 10 mg IV now."),
        ("Please hold heparin tonight, thanks.", "Please hold tonight, thanks."),
        ("No hepatosplenomegaly on exam today.", "No splenomegaly on exam today."),
        # Measured: a sentence left out, and one added that wasn't said.
        ("Hi Sarah, I wanted to follow up on yesterday's meeting. Could you send me the updated schedule by Friday afternoon? Thanks again for your help.",
         "Hi Sarah,\n\nCould you send me the updated schedule by Friday afternoon? Thanks again for your help.\n\n---\n\n"
         "I meant to say, could you send me the updated schedule by Friday? Thanks again for your help."),
        ("Start apixaban 5 mg twice a day and furosemide 40 mg intravenously.",  # measured, with medical off
         "Apixaban 5 mg twice a day. Furosemide 40 mg intravenously."),
        ("Family is at the bedside and agrees with the plan. Hold heparin tonight and restart it in the morning.",
         "Hold heparin tonight and restart it in the morning."),
    ]:
        monkeypatch.setattr(ai_cleanup, "_post", lambda *a, answer=answer, **k: {"message": {"content": answer}})
        assert ai_cleanup.refine(said, s, context.DOCUMENT) == said
    # Rewording that keeps them is fine.
    monkeypatch.setattr(ai_cleanup, "_post", lambda *a, **k: {"message": {"content": "Metoprolol discontinued. Heparin held tonight."}})
    assert ai_cleanup.refine("stop metoprolol. hold heparin tonight", s, context.DOCUMENT) == "Metoprolol discontinued. Heparin held tonight."
    monkeypatch.setattr(ai_cleanup, "_post", lambda *a, **k: {"message": {"content": "He does not smoke. Abdomen non-tender."}})
    assert ai_cleanup.refine("he doesn't smoke, abdomen nontender", s, context.DOCUMENT) == "He does not smoke. Abdomen non-tender."
    monkeypatch.setattr(ai_cleanup, "_post", lambda *a, **k: {"message": {"content": "Start apixaban 5 mg twice a day."}})
    assert ai_cleanup.refine("start a pyxibon 5 mg twice a day", s, context.DOCUMENT) == "Start apixaban 5 mg twice a day."
    for said, answer in [
        ("Plan: number one, start aspirin. Number two, hold heparin.", "Plan:\n1. Start aspirin.\n2. Hold heparin."),
        ("The patient is a 67 year old man with hypertension on metoprolol 25 mg twice daily.",
         "A 67-year-old man with hypertension on metoprolol 25 mg twice daily."),
    ]:
        monkeypatch.setattr(ai_cleanup, "_post", lambda *a, answer=answer, **k: {"message": {"content": answer}})
        assert ai_cleanup.refine(said, s, context.DOCUMENT) == answer


def test_markdown_the_ai_adds_is_removed(monkeypatch):
    """Measured with qwen2.5:3b: drug names came back in **bold** with markdown line breaks."""
    s = config.merge(config.DEFAULTS, {})
    said = "Continue nimodipine 60 mg every 4 hours. Start a nicardipine infusion."
    monkeypatch.setattr(ai_cleanup, "_post", lambda *a, **k: {"message": {"content": "Continue **nimodipine** 60 mg every 4 hours.  \nStart a **nicardipine** infusion."}})
    assert ai_cleanup.refine(said, s, context.DOCUMENT) == "Continue nimodipine 60 mg every 4 hours.\nStart a nicardipine infusion."
    assert "markdown" in ai_cleanup.build_system_prompt(s, context.DOCUMENT)
    assert "PO BID" not in ai_cleanup.build_system_prompt(s, context.DOCUMENT)  # the example the model copied


def test_the_instructions_that_never_change_come_first():
    """Ollama reuses what it already read when a request starts the same way as the last one."""
    s = config.merge(config.DEFAULTS, {"dictionary": ["Farghal"]})
    s["ai_cleanup"]["extra_instructions"] = "Use British spelling."
    warm_up = ai_cleanup.build_system_prompt(s, context.DOCUMENT)
    assert "Farghal" in warm_up and "British spelling" in warm_up and "Medical writing rules" in warm_up
    for text in ("start metaprolol tomorrow", "give keprah tonight"):
        prompt = ai_cleanup.build_system_prompt(s, context.DOCUMENT, text=text, before="Plan:")
        assert prompt.startswith(warm_up) and len(prompt) > len(warm_up)


class _Ollama:
    """A local server that streams an answer the way Ollama does, one JSON line at a time."""

    def __init__(self, first_after: float, lines: list[str], every: float = 0.0):
        import http.server
        import json
        import threading
        import time

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # chunked, like Ollama

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                time.sleep(first_after)
                for i, text in enumerate(lines):
                    data = (json.dumps({"message": {"content": text}, "done": i == len(lines) - 1}) + "\n").encode()
                    self.wfile.write(b"%X\r\n%s\r\n" % (len(data), data))
                    self.wfile.flush()
                    time.sleep(every)
                self.wfile.write(b"0\r\n\r\n")

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_a_long_answer_may_finish_but_a_slow_start_fails_fast():
    import time

    import pytest

    s = config.merge(config.DEFAULTS, {})
    words = ["word "] * 20
    server = _Ollama(first_after=0.05, lines=words, every=0.06)  # 1.2 s in all, more than the 1 s limit
    try:
        s["ai_cleanup"]["ollama_url"] = server.url
        assert ai_cleanup._chat(s, "system", "user", 1, budget=5) == ("word " * 20).strip()
        with pytest.raises(TimeoutError):
            ai_cleanup._chat(s, "system", "user", 1, budget=0.5)
    finally:
        server.close()
    server = _Ollama(first_after=1.5, lines=["late"])
    try:
        s["ai_cleanup"]["ollama_url"] = server.url
        t = time.monotonic()
        with pytest.raises(OSError):  # a socket timeout: the model didn't start within the limit
            ai_cleanup._chat(s, "system", "user", 0.5, budget=30)
        assert time.monotonic() - t < 1.2
    finally:
        server.close()


def test_warm_up_reads_the_instructions(monkeypatch):
    seen = []
    monkeypatch.setattr(ai_cleanup, "_post", lambda url, payload, timeout, *a: seen.append((url, payload)) or {"message": {"content": "OK"}})
    s = config.merge(config.DEFAULTS, {})
    ai_cleanup.warm_up(s, context.EMAIL)
    url, payload = seen[0]
    assert url.endswith("/api/chat") and payload["options"]["num_predict"] == 1
    assert payload["messages"][0]["content"] == ai_cleanup.build_system_prompt(s, context.EMAIL)


def test_prewarm_only_when_the_model_may_have_been_let_go(monkeypatch):
    import threading
    import time

    calls = []
    done = threading.Event()
    monkeypatch.setattr(ai_cleanup, "warm_up", lambda s, app_type: (calls.append(app_type), done.set()))
    s = config.merge(config.DEFAULTS, {})
    monkeypatch.setattr(ai_cleanup, "_last_answer", time.monotonic())  # answered a moment ago
    monkeypatch.setattr(ai_cleanup, "_last_warm_up", float("-inf"))
    ai_cleanup.prewarm(s, context.EMAIL)
    monkeypatch.setattr(ai_cleanup, "_last_answer", time.monotonic() - 31 * 60)
    ai_cleanup.prewarm(s, context.EMAIL)
    ai_cleanup.prewarm(s, context.EMAIL)  # a second dictation right after: already on its way
    assert done.wait(2) and calls == [context.EMAIL]
