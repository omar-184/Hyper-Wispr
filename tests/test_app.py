import threading

from hyper_wispr import app as app_module
from hyper_wispr import config, context
from hyper_wispr.app import App


def bare_app(**state):
    """An App without a microphone, tray icon or speech model."""
    a = object.__new__(App)
    a._status_lock = threading.Lock()
    a.recording, a.pending, a.transcriber, a.load_error = False, 0, object(), ""
    a.statuses = []
    a.set_status = a.statuses.append
    a.notices = []
    a.notify = lambda title, message: a.notices.append(title)
    a.settings = config.merge(config.DEFAULTS, {})
    a.settings["ai_cleanup"]["enabled"] = False
    a.last_text = ""
    a.wait_for_keys_released = lambda: None
    for k, v in state.items():
        setattr(a, k, v)
    return a


def test_finishing_a_dictation_keeps_the_next_recording_visible():
    a = bare_app(recording=True, pending=1)  # dictating again while the last one is typed
    with a._status_lock:
        a.pending -= 1
    a._settle_status()
    assert a.statuses[-1] == "recording"


def test_status_after_the_last_dictation_is_ready_or_still_working():
    a = bare_app(pending=2)
    a._settle_status()
    assert a.statuses[-1] == "working"
    a.pending = 0
    a._settle_status()
    assert a.statuses[-1] == "idle"
    a.transcriber, a.load_error = None, "no internet"
    a._settle_status()
    assert a.statuses[-1] == "error"


class FakeTranscriber:
    def transcribe(self, audio):
        return "the patient is stable"


class FakeHistory:
    def __init__(self):
        self.rows = []

    def add(self, *row):
        self.rows.append(row)


def test_text_is_still_typed_when_cleanup_breaks(monkeypatch):
    a = bare_app(transcriber=FakeTranscriber(), history=FakeHistory())
    typed = []
    monkeypatch.setattr(app_module, "process_text", lambda *args: 1 / 0)
    monkeypatch.setattr(app_module.paste, "insert", lambda text, settings, app="": typed.append(text))
    monkeypatch.setattr(app_module.context, "current", lambda: context.AppContext(process="WINWORD.EXE"))
    a.dictate(None, 2.0, context.AppContext(process="WINWORD.EXE"), 0.0)
    assert typed == ["the patient is stable"]
    assert a.history.rows and a.last_text == "the patient is stable"


def test_failed_paste_keeps_the_text_and_says_so(monkeypatch):
    a = bare_app(transcriber=FakeTranscriber(), history=FakeHistory())

    def broken(*args, **kwargs):
        raise OSError("The clipboard is busy")

    monkeypatch.setattr(app_module.paste, "insert", broken)
    monkeypatch.setattr(app_module.context, "current", lambda: context.AppContext())
    monkeypatch.setattr(app_module, "chime", lambda *args: None)
    a.dictate(None, 2.0, context.AppContext(), 0.0)
    assert a.history.rows  # saved before the paste was tried
    assert a.last_text == "The patient is stable"
    assert a.notices == ["Couldn't type the text"]


def test_damaged_history_file_is_set_aside(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)
    (tmp_path / "history.sqlite3").write_bytes(b"this is not a database" * 100)
    history = app_module._open_history()
    history.add("WINWORD.EXE", "document", 1.0, "raw", "Final.", 0.2)
    assert history.last() == "Final."
    assert (tmp_path / "history.broken.sqlite3").exists()
