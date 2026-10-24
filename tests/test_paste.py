import sys
from types import SimpleNamespace

import pytest

from hyper_wispr import clipboard, paste


class FakeClipboard:
    """The Windows clipboard as the paste code sees it."""

    def __init__(self, contents):
        self.contents, self.seq = contents, 1

    def snapshot(self):
        return list(self.contents)

    def set_text(self, text):
        self.contents, self.seq = [(13, text)], self.seq + 1
        return self.seq

    def restore(self, items):
        self.contents, self.seq = list(items), self.seq + 1

    def sequence(self):
        return self.seq

    def user_copies(self, item):
        self.contents, self.seq = [item], self.seq + 1


@pytest.fixture
def board(monkeypatch):
    fake = FakeClipboard([(8, b"screenshot")])  # an image, which pyperclip used to wipe
    for name in ("snapshot", "set_text", "restore", "sequence"):
        monkeypatch.setattr(clipboard, name, getattr(fake, name))
    pasted = []
    monkeypatch.setattr(paste, "_paste_shortcut", lambda: pasted.append(fake.contents[0][1]))
    monkeypatch.setattr(paste, "_pending", None)
    monkeypatch.setattr(paste, "RESTORE_DELAY", 0)
    # Restores run when the test says so, not on a timer thread.
    started = []
    monkeypatch.setattr(paste, "threading", SimpleNamespace(Thread=lambda target, args, daemon: SimpleNamespace(start=lambda: started.append(args))))
    fake.pasted, fake.started = pasted, started
    return fake


def test_clipboard_is_put_back_after_pasting(board):
    paste._insert_windows("Dictated.", restore=True)
    assert board.pasted == ["Dictated."]
    paste._restore_later(*board.started[0])
    assert board.contents == [(8, b"screenshot")]


def test_something_copied_meanwhile_is_not_overwritten(board):
    paste._insert_windows("Dictated.", restore=True)
    board.user_copies((13, "copied on purpose"))
    paste._restore_later(*board.started[0])
    assert board.contents == [(13, "copied on purpose")]


def test_two_quick_dictations_still_restore_the_original(board):
    paste._insert_windows("First.", restore=True)
    paste._insert_windows("Second.", restore=True)
    for args in board.started:
        paste._restore_later(*args)
    assert board.pasted == ["First.", "Second."]
    assert board.contents == [(8, b"screenshot")]


def test_remote_desktop_windows_keep_the_dictation_on_the_clipboard(board, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    settings = {"paste": {"method": "clipboard", "restore_clipboard": True, "add_trailing_space": True}}
    paste.insert("Dictated.", settings, app="WFICA32.EXE")
    assert board.pasted == ["Dictated. "] and board.started == []


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows clipboard")
def test_real_windows_clipboard_round_trip():
    try:
        saved = clipboard.snapshot()
        clipboard.set_text("line one\nline two")
        assert clipboard.get_text() == "line one\r\nline two"
        clipboard.restore(saved or [])
    except OSError as e:  # no clipboard on this machine (e.g. a service session)
        pytest.skip(str(e))
