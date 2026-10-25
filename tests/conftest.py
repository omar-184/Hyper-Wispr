import pytest

from hyper_wispr import local_llm


@pytest.fixture(autouse=True)
def _ollama_by_default(monkeypatch, request):
    """Most AI tests talk to a stand-in Ollama; they mustn't reach the built-in engine on a machine
    that happens to have llama-cpp-python. Tests of the built-in engine are marked builtin_ai."""
    if "builtin_ai" not in request.keywords:
        monkeypatch.setattr(local_llm, "installed", lambda: False)
