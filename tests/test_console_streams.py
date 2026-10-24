import sys

import pytest

from hyper_wispr.__main__ import give_streams_somewhere_to_go


@pytest.fixture
def no_console(monkeypatch):
    """Like the installed app: started without a console, so there is no stdout or stderr."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.delenv("HF_HUB_DISABLE_PROGRESS_BARS", raising=False)


def test_printing_without_a_console_does_not_crash(no_console):
    give_streams_somewhere_to_go()
    print("hello")
    sys.stderr.write("progress 50%\r")


def test_first_model_download_progress_bar_does_not_crash_without_a_console(no_console):
    # The 0.4.0 bug: "Speech model didn't load: 'NoneType' object has no attribute 'write'".
    tqdm = pytest.importorskip("tqdm").tqdm
    give_streams_somewhere_to_go()
    for _ in tqdm(range(3)):
        pass


def test_download_progress_bars_are_turned_off(no_console):
    import os

    give_streams_somewhere_to_go()
    assert os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] == "1"


def test_a_real_console_is_left_alone(monkeypatch):
    out, err = sys.stdout, sys.stderr
    give_streams_somewhere_to_go()
    assert sys.stdout is out and sys.stderr is err
