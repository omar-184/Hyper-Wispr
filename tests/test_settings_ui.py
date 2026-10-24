import json

import pytest

from hyper_wispr import config
from hyper_wispr.history import History
from hyper_wispr.settings_ui import Api


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(config, "settings_path", lambda: tmp_path / "settings.json")
    return tmp_path


def test_state_creates_defaults(data_dir):
    state = Api().get_state()
    assert state["settings"] == config.DEFAULTS
    assert state["load_error"] == ""
    assert (data_dir / "settings.json").exists()


def test_broken_settings_file_shows_defaults_and_says_so(data_dir):
    (data_dir / "settings.json").write_text("{ not json", encoding="utf-8")
    state = Api().get_state()
    assert state["settings"] == config.DEFAULTS
    assert "could not be read" in state["load_error"]


def test_save_fills_missing_keys_and_leaves_no_temp_file(data_dir):
    assert Api().save_settings({"speech": {"languages": ["en", "ar"]}, "dictionary": ["apixaban"]})["ok"]
    saved = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    assert saved["speech"]["languages"] == ["en", "ar"]
    assert saved["speech"]["model"] == config.DEFAULTS["speech"]["model"]
    assert saved["dictionary"] == ["apixaban"]
    assert not list(data_dir.glob("*.tmp"))


def test_history_read_and_delete(data_dir):
    api = Api()
    assert api.get_history() == []
    h = History(data_dir / "history.sqlite3")
    h.add("WINWORD.EXE", "document", 2.0, "raw one", "First.", 0.5)
    h.add("OUTLOOK.EXE", "email", 3.0, "raw two", "Second.", 0.7)
    items = api.get_history()
    assert [i["text"] for i in items] == ["Second.", "First."]
    assert api.delete_history(items[0]["id"])
    assert [i["text"] for i in api.get_history()] == ["First."]
    assert api.delete_history(None)
    assert api.get_history() == []


def test_ollama_status_when_not_running():
    status = Api().ollama_status("http://127.0.0.1:9")
    assert status["running"] is False and status["models"] == []


def test_start_at_login_needs_installed_app():
    # Tests run from source, not the installed exe, so this must refuse rather than make a broken shortcut.
    assert Api().set_start_at_login(True) is False


def test_diff_lists_only_what_changed():
    base = {"speech": {"model": "auto", "languages": ["en"]}, "replacements": {"a": "b"}, "sounds": True}
    new = {"speech": {"model": "small", "languages": ["en"]}, "replacements": {}, "sounds": True}
    assert config.diff(base, new) == {"speech.model": "small", "replacements": {}}


def test_window_save_keeps_a_change_made_meanwhile_from_the_tray(data_dir):
    api = Api()
    loaded = api.get_state()["settings"]  # what the window shows
    config.update({"ai_cleanup.enabled": False})  # tray menu: AI cleanup off
    edited = json.loads(json.dumps(loaded))
    edited["speech"]["languages"] = ["en", "ar"]  # meanwhile, in the window
    assert api.save_settings(edited, loaded)["ok"]
    saved = config.load()
    assert saved["speech"]["languages"] == ["en", "ar"]
    assert saved["ai_cleanup"]["enabled"] is False


def test_saving_over_a_broken_file_keeps_a_backup(data_dir):
    (data_dir / "settings.json").write_text("{ not json", encoding="utf-8")
    api = Api()
    shown = api.get_state()["settings"]
    edited = json.loads(json.dumps(shown))
    edited["audio"]["sounds"] = False
    assert api.save_settings(edited, shown)["ok"]
    assert config.load()["audio"]["sounds"] is False
    assert (data_dir / "settings.broken.json").read_text(encoding="utf-8") == "{ not json"


def test_unchanged_save_does_not_touch_the_file(data_dir):
    api = Api()
    loaded = api.get_state()["settings"]
    before = (data_dir / "settings.json").stat().st_mtime_ns
    assert api.save_settings(loaded, loaded)["ok"]
    assert (data_dir / "settings.json").stat().st_mtime_ns == before


def test_copy_uses_the_private_clipboard(monkeypatch):
    copied = []
    monkeypatch.setattr("hyper_wispr.paste.copy", copied.append)
    assert Api().copy_text("Metoprolol 25 mg PO BID.")
    assert copied == ["Metoprolol 25 mg PO BID."]


def test_shortcut_capture_flag(monkeypatch):
    import sys

    from hyper_wispr import settings_ui

    api = Api()
    api.begin_shortcut_capture()
    try:
        assert settings_ui.shortcut_capture_active() is (sys.platform == "win32")
    finally:
        api.end_shortcut_capture()
    assert settings_ui.shortcut_capture_active() is False


def test_no_settings_window_to_focus():
    from hyper_wispr import settings_ui

    assert settings_ui.focus_window() is False
