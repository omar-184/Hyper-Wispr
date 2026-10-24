"""Features added in 0.4: context, styles, command mode, phantoms, learning, history, notes."""

import time

import numpy as np
import pytest

from hyper_wispr import ai_cleanup, cleanup, config, context, learn, speech
from hyper_wispr import app as app_module
from hyper_wispr.__main__ import trim_log
from hyper_wispr.app import process_text
from hyper_wispr.history import History
from hyper_wispr.hotkeys import HANDS_FREE, HotkeyStateMachine


def settings(**over):
    s = config.merge(config.DEFAULTS, over)
    s["ai_cleanup"]["enabled"] = False
    return s


# ---- the text around the cursor ------------------------------------------------

def test_dictation_continues_a_sentence():
    text, space = cleanup.fit_to_context("The patient is stable.", "Overnight", "")
    assert text == " the patient is stable." and space


def test_names_and_i_keep_their_capital_mid_sentence():
    assert cleanup.fit_to_context("Apixaban was started.", "Yesterday ", "")[0] == "Apixaban was started."
    assert cleanup.fit_to_context("I saw her.", "and then ", "")[0] == "I saw her."


def test_after_a_full_stop_nothing_changes():
    assert cleanup.fit_to_context("The patient is stable.", "No events. ", "")[0] == "The patient is stable."


def test_no_double_space_or_early_full_stop_before_existing_text():
    text, space = cleanup.fit_to_context("twice daily.", "Give it ", ", with food")
    assert text == "twice daily" and not space


def test_unknown_context_changes_nothing():
    assert cleanup.fit_to_context("The patient is stable.", "", "") == ("The patient is stable.", True)


def test_process_text_uses_context():
    out = process_text("the plan is to discharge tomorrow", settings(), context.DOCUMENT, before="After review,")
    assert out == " the plan is to discharge tomorrow"


# ---- styles ------------------------------------------------------------------

def test_very_casual_drops_capital_and_full_stop():
    assert cleanup.apply_style("The meeting is at 5.", "very_casual") == "the meeting is at 5"
    assert cleanup.apply_style("Sarah is here.", "very_casual") == "Sarah is here"


def test_casual_single_sentence_loses_final_full_stop_only():
    assert cleanup.apply_style("See you soon.", "casual") == "See you soon"
    assert cleanup.apply_style("See you soon. Bring it.", "casual") == "See you soon. Bring it."
    assert cleanup.apply_style("See you soon.", "formal") == "See you soon."


def test_style_and_context_reach_the_ai_prompt():
    s = settings()
    s["ai_cleanup"]["styles"]["message"] = "very_casual"
    prompt = ai_cleanup.build_system_prompt(s, context.MESSAGE, before="Hi Dr. Farghal,")
    assert ai_cleanup.TONES["very_casual"] in prompt
    assert "Dr. Farghal" in prompt


def test_starred_words_come_first_without_repeats():
    s = settings(dictionary=["metoprolol", "Apixaban"], dictionary_starred=["apixaban"])
    assert ai_cleanup.dictionary_words(s) == ["apixaban", "metoprolol"]


# ---- command mode ------------------------------------------------------------

def test_command_rewrites_selection(monkeypatch):
    seen = {}

    def post(url, payload, timeout, *a):
        seen["user"] = payload["messages"][1]["content"]
        return {"message": {"content": "```\nPatient stable. Discharge tomorrow.\n```"}}

    monkeypatch.setattr(ai_cleanup, "_post", post)
    out = ai_cleanup.command("make this shorter", "The patient is doing well and stable. We will discharge tomorrow.", settings())
    assert out == "Patient stable. Discharge tomorrow."
    assert "make this shorter" in seen["user"]


def test_command_that_changes_a_dose_is_refused(monkeypatch):
    monkeypatch.setattr(ai_cleanup, "_post", lambda *a, **k: {"message": {"content": "Metoprolol 50 mg BID."}})
    with pytest.raises(ai_cleanup.CommandError):
        ai_cleanup.command("tidy this up", "metoprolol 25 mg twice a day", settings())


def test_command_when_ollama_is_down(monkeypatch):
    def down(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(ai_cleanup, "_post", down)
    with pytest.raises(ai_cleanup.CommandError):
        ai_cleanup.command("make it formal", "hey", settings())


def make_machine(events):
    return HotkeyStateMachine(
        ["ctrl_l", "cmd"], 0.35,
        on_start=lambda: events.append("start"), on_stop=lambda: events.append("stop"),
        on_cancel=lambda: events.append("cancel"), command_key="alt_l",
        paste_last=["ctrl_l", "alt_l", "z"], on_paste_last=lambda: events.append("paste_last"),
        clock=iter(range(0, 10_000, 5)).__next__,  # every key press "takes" 5 s: no taps
    )


def test_command_key_before_or_during_recording():
    ev = []
    m = make_machine(ev)
    for k in ("alt_l", "ctrl_l", "cmd"):
        m.key_down(k)
    assert ev == ["start"] and m.command
    for k in ("cmd", "ctrl_l", "alt_l"):
        m.key_up(k)
    assert ev == ["start", "stop"]
    m.key_down("ctrl_l")
    m.key_down("cmd")
    assert not m.command  # a new, plain dictation
    m.key_down("alt_l")
    assert m.command


def test_alt_in_hands_free_mode_keeps_it_a_dictation():
    """Alt+Tab to another window while dictating hands-free mustn't turn the words into a command
    (they were lost, or rewrote whatever was selected there)."""
    ev = []
    t = [0.0]
    m = HotkeyStateMachine(["ctrl_l", "cmd"], 0.35, on_start=lambda: ev.append("start"),
                           on_stop=lambda: ev.append("stop"), on_cancel=lambda: ev.append("cancel"),
                           command_key="alt_l", clock=lambda: t[0])
    m.key_down("ctrl_l")
    m.key_down("cmd")
    m.key_up("cmd")
    m.key_up("ctrl_l")  # a quick tap: hands-free
    assert m.state == HANDS_FREE
    t[0] = 5.0
    m.key_down("alt_l")
    m.key_down("tab")
    m.key_up("tab")
    m.key_up("alt_l")
    m.key_down("ctrl_l")
    m.key_down("cmd")
    assert ev == ["start", "stop"] and not m.command


def test_paste_last_shortcut():
    ev = []
    m = make_machine(ev)
    for k in ("ctrl_l", "alt_l", "z"):
        m.key_down(k)
    assert ev == ["paste_last"]


# ---- phantom phrases ---------------------------------------------------------

def test_phantom_phrases_are_dropped():
    assert speech.drop_phantoms([("Thank you.", 0.6)]) == ""
    assert speech.drop_phantoms([("ترجمة نانسي قنقر", 0.0)]) == ""
    assert speech.drop_phantoms([("The patient is stable.", 0.05), (" Subtitles by the Amara.org community", 0.1)]) == "The patient is stable."


def test_real_thank_you_is_kept():
    assert speech.drop_phantoms([("Thank you.", 0.02)]) == "Thank you."
    assert speech.drop_phantoms([("Please send it.", 0.01), ("Thank you.", 0.05)]) == "Please send it. Thank you."


def test_whisper_mode_amplifies_much_more():
    soft = 0.0005 * np.sin(np.linspace(0, 200, 16000)).astype(np.float32)
    assert np.max(np.abs(speech.prepare_audio(soft))) < 0.02
    assert np.max(np.abs(speech.prepare_audio(soft, whisper_mode=True))) > 0.05


# ---- learning words ----------------------------------------------------------

def test_corrected_word_is_found():
    found = learn.corrected_words("Please call doctor Fargal tomorrow.", "Notes. Please call doctor Farghal tomorrow. Thanks", set())
    assert found == [("Fargal", "Farghal")]


def test_rewording_and_known_words_are_not_learned():
    assert learn.corrected_words("Please call doctor Fargal tomorrow.", "Please phone the doctor today.", set()) == []
    assert learn.corrected_words("Please call doctor Fargal tomorrow.", "Please call doctor Farghal tomorrow.", {"farghal"}) == []


# ---- history -----------------------------------------------------------------

def test_history_search_redo_audio_and_retention(tmp_path):
    h = History(tmp_path / "h.sqlite3")
    audio = np.linspace(-0.5, 0.5, 1600).astype(np.float32)
    first = h.add("WINWORD.EXE", "document", 2.0, "raw", "Apixaban 5 mg started.", 0.5, audio=audio, keep_recordings=1)
    second = h.add("OUTLOOK.EXE", "email", 1.0, "raw", "See you Monday.", 0.5, audio=audio, keep_recordings=1)
    assert [i["text"] for i in h.recent(search="apixaban")] == ["Apixaban 5 mg started."]
    assert h.recording(first) is None  # only the newest recording is kept
    assert np.allclose(h.recording(second), audio, atol=1e-4)
    h.update_text(second, "See you on Monday.")
    assert h.get(second)["text"] == "See you on Monday."
    assert h.delete_older_than(30, now=time.time() + 31 * 86400) == 2
    assert h.recent() == [] and h.recording(second) is None


def test_usage_totals_survive_deleting_history(tmp_path):
    h = History(tmp_path / "h.sqlite3")
    h.add("WINWORD.EXE", "document", 3.0, "raw", "one two three", 0.5)
    h.count_usage(2.0, "four five")  # history switched off for this one
    h.delete(None)
    [day] = h.daily()
    assert day["dictations"] == 2 and day["words"] == 5


def test_notes_and_suggestions(tmp_path):
    h = History(tmp_path / "h.sqlite3")
    nid = h.save_note("Ward round list")
    h.save_note("Ward round list\n- bed 4", nid)
    assert [n["body"] for n in h.notes()] == ["Ward round list\n- bed 4"]
    h.suggest("Farghal", "Fargal")
    h.suggest("Farghal", "Fargal")
    assert h.suggestions() == [{"word": "Farghal", "heard": "Fargal", "count": 2}]
    h.dismiss_suggestion("Farghal")
    assert h.suggestions() == []
    h.delete_note(nid)
    assert h.notes() == []


def test_unused_keep_audio_setting_is_removed(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"history": {"enabled": true, "keep_audio": true}}')
    s = config.load(path)
    assert "keep_audio" not in s["history"] and s["history"]["keep_days"] == 30


def test_big_log_is_set_aside(tmp_path):
    log = tmp_path / "hyper-wispr.log"
    log.write_bytes(b"x" * 100)
    trim_log(log, limit=50)
    assert not log.exists() and (tmp_path / "hyper-wispr.old.log").exists()


# ---- safety net --------------------------------------------------------------

def test_dictation_with_no_text_box_goes_to_the_clipboard(monkeypatch):
    from test_app import FakeHistory, FakeTranscriber, bare_app

    a = bare_app(transcriber=FakeTranscriber(), history=FakeHistory())
    pasted, copied = [], []
    monkeypatch.setattr(app_module.paste, "insert", lambda *a, **k: pasted.append(a[0]))
    monkeypatch.setattr(app_module.paste, "copy", copied.append)
    monkeypatch.setattr(app_module.context, "current", lambda: context.AppContext(process="explorer.exe", window_class="Progman"))
    a.dictate(None, 2.0, context.AppContext(), 0.0)
    assert pasted == [] and copied == ["The patient is stable"]
    assert a.notices == ["No text box selected"]


def test_deleting_a_dictation_wipes_its_text_from_the_file(tmp_path):
    """Without rewriting the whole file each time (0.4 s per delete with a month of history)."""
    path = tmp_path / "h.sqlite3"
    h = History(path)
    keep = h.add("WINWORD.EXE", "document", 2.0, "raw", "Keep this one.", 0.5)
    gone = h.add("WINWORD.EXE", "document", 2.0, "Bed 7 Zylpharon", "Bed 7 Zylpharon started.", 0.5)
    h.delete(gone)
    h.close()
    assert b"Zylpharon" not in path.read_bytes()  # on Windows it's encrypted too
    h = History(path)
    assert [i["id"] for i in h.recent()] == [keep]


def test_search_decrypts_each_dictation_once(tmp_path, monkeypatch):
    from hyper_wispr import history as history_module

    h = History(tmp_path / "h.sqlite3")
    for i in range(20):
        h.add("WINWORD.EXE", "document", 2.0, "raw", f"Dictation {i} about apixaban.", 0.5)
    calls = []
    decrypt = history_module.secure.decrypt_text
    monkeypatch.setattr(history_module.secure, "decrypt_text", lambda v: calls.append(1) or decrypt(v))
    assert len(h.recent(search="apixaban")) == 20
    first = len(calls)
    assert len(h.recent(search="dictation 17")) == 1 and len(calls) == first
    newest = h.recent(1)[0]["id"]
    h.update_text(newest, "Now about heparin.")
    assert [i["id"] for i in h.recent(search="heparin")] == [newest]
