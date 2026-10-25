import copy
import os

import pytest

from hyper_wispr import ai_cleanup, config, local_llm


def _settings(**ai):
    s = copy.deepcopy(config.DEFAULTS)
    s["ai_cleanup"].update(ai)
    return s


def test_guesses_come_from_where_the_dictation_has_got_to():
    source = [1, 2, 3, 9, 1, 2, 4, 5, 6]
    # "1 2" is in the dictation twice; past position 3 the second one is the one being cleaned.
    assert local_llm.lookup([7, 1, 2], source, 4, 3) == ([4, 5, 6], 6)
    assert local_llm.lookup([7, 1, 2], source, 0, 3) == ([3, 9, 1], 2)


def test_a_longer_match_wins_and_nothing_matching_guesses_nothing():
    source = [5, 1, 2, 8, 3, 1, 2, 9]
    assert local_llm.lookup([3, 1, 2], source, 0, 2) == ([9], 7)
    assert local_llm.lookup([42], source, 0, 4) == ([], 0)


def test_ollama_is_used_where_the_built_in_engine_cant_run(monkeypatch):
    monkeypatch.setattr(local_llm, "installed", lambda: True)
    monkeypatch.setattr(local_llm, "cpu_supported", lambda: False)
    assert local_llm.engine({"engine": "builtin"}) == "ollama"
    assert "AVX2" in local_llm.why_not()
    monkeypatch.setattr(local_llm, "cpu_supported", lambda: True)
    assert local_llm.engine({"engine": "builtin"}) == "builtin"
    assert local_llm.engine({"engine": "ollama"}) == "ollama"


@pytest.mark.builtin_ai
def test_while_the_model_downloads_ollama_answers_if_running(monkeypatch):
    monkeypatch.setattr(local_llm, "installed", lambda: True)
    monkeypatch.setattr(local_llm, "cpu_supported", lambda: True)
    monkeypatch.setattr(local_llm, "model_path", lambda key: None)
    s = _settings()
    monkeypatch.setattr(ai_cleanup, "is_available", lambda settings: True)
    assert not ai_cleanup.uses_builtin(s)
    monkeypatch.setattr(ai_cleanup, "is_available", lambda settings: False)
    assert ai_cleanup.uses_builtin(s)  # and fails fast, so the basic cleanup is pasted
    monkeypatch.setattr(local_llm, "model_path", lambda key: "/models/x.gguf")
    monkeypatch.setattr(ai_cleanup, "is_available", lambda settings: True)
    assert ai_cleanup.uses_builtin(s)


@pytest.mark.builtin_ai
def test_cleanup_goes_to_the_built_in_engine_with_the_dictation_to_guess_from(monkeypatch):
    monkeypatch.setattr(local_llm, "installed", lambda: True)
    monkeypatch.setattr(local_llm, "cpu_supported", lambda: True)
    monkeypatch.setattr(local_llm, "model_path", lambda key: "/models/x.gguf")
    seen = {}

    def chat(cfg, system, user, timeout, budget=None, max_tokens=512, source=None):
        seen.update(source=source, max_tokens=max_tokens, timeout=timeout, budget=budget)
        return "Start apixaban 5 mg twice a day."

    monkeypatch.setattr(local_llm, "chat", chat)
    said = "start apixaban 5 mg twice a day"
    assert ai_cleanup.refine(said, _settings(), "document") == "Start apixaban 5 mg twice a day."
    assert seen["source"] == said and seen["max_tokens"] >= 64 and seen["budget"] > seen["timeout"]


@pytest.mark.builtin_ai
def test_a_failing_built_in_engine_keeps_the_text(monkeypatch):
    monkeypatch.setattr(local_llm, "installed", lambda: True)
    monkeypatch.setattr(local_llm, "cpu_supported", lambda: True)
    monkeypatch.setattr(local_llm, "model_path", lambda key: "/models/x.gguf")

    def chat(*a, **k):
        raise RuntimeError("the AI helper stopped")

    monkeypatch.setattr(local_llm, "chat", chat)
    assert ai_cleanup.refine("the patient is doing well today", _settings(), "document") == "the patient is doing well today"


def test_the_default_model_is_one_of_the_catalog():
    assert config.DEFAULTS["ai_cleanup"]["builtin_model"] == local_llm.DEFAULT_MODEL
    assert local_llm.DEFAULT_MODEL in local_llm.MODELS
    for m in local_llm.MODELS.values():
        assert m["file"].endswith(".gguf") and m["repo"].count("/") == 1


def test_a_gguf_file_of_your_own(tmp_path):
    f = tmp_path / "mine.gguf"
    assert local_llm.model_path(str(f)) is None
    f.write_bytes(b"GGUF")
    assert local_llm.model_path(str(f)) == str(f)


# A real model through the helper process: set HYPER_WISPR_TEST_GGUF to a .gguf file (any size works,
# a random tiny one too: the test compares the answer with and without guessing ahead).
GGUF = os.environ.get("HYPER_WISPR_TEST_GGUF")


@pytest.mark.builtin_ai
@pytest.mark.skipif(not (GGUF and local_llm.installed()), reason="needs llama-cpp-python and HYPER_WISPR_TEST_GGUF")
def test_guessing_ahead_writes_exactly_what_the_model_would_write():
    eng = local_llm.Engine(GGUF, n_ctx=1024)
    user = "<dictation>\nthe patient is doing well and denies chest pain\n</dictation>"
    plain = eng.chat("Clean up the dictation.", user, max_tokens=48, draft=0)
    for source in (plain["text"], "the patient is doing well and denies chest pain"):
        fast = eng.chat("Clean up the dictation.", user, max_tokens=48, draft=8, source=source)
        assert fast["text"] == plain["text"]
        assert fast["stats"]["reused_tokens"] == fast["stats"]["prompt_tokens"] - 1


@pytest.mark.builtin_ai
@pytest.mark.skipif(not (GGUF and local_llm.installed()), reason="needs llama-cpp-python and HYPER_WISPR_TEST_GGUF")
def test_the_helper_process_answers_and_comes_back_after_a_crash():
    s = _settings(builtin_model=GGUF)
    try:
        assert local_llm.chat(s["ai_cleanup"], "sys", "hello", 30, 60, max_tokens=4) is not None
        local_llm._helper.proc.kill()
        local_llm._helper.proc.wait()
        assert local_llm.chat(s["ai_cleanup"], "sys", "hello", 30, 60, max_tokens=4) is not None
    finally:
        local_llm.close()


def test_the_window_shows_the_built_in_models():
    from hyper_wispr.settings_ui import Api

    st = Api().ai_status()
    assert {m["key"] for m in st["models"]} == set(local_llm.MODELS)
    assert st["default"] == local_llm.DEFAULT_MODEL and isinstance(st["usable"], bool)


def test_the_tray_says_which_model_downloads():
    from hyper_wispr.app import _download_label

    assert _download_label({"done": 5e8, "total": 2e9, "what": "ai"}) == "Downloading the AI cleanup model: 25% of 2.0 GB"
