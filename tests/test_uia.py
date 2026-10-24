import importlib.abc
import importlib.machinery
import sys

from hyper_wispr import uia


def test_com_is_multithreaded_from_the_first_import_of_comtypes(monkeypatch):
    """comtypes sets up COM on the thread that first imports it, single-threaded unless
    sys.coinit_flags says otherwise, and the UI Automation thread then couldn't make it
    multithreaded: measured on Windows, every UI Automation call failed."""
    seen = []

    class FakeComtypes(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, name, path, target=None):
            return importlib.machinery.ModuleSpec(name, self) if name == "comtypes" else None

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            seen.append(("import", getattr(sys, "coinit_flags", 2)))  # what comtypes passes to CoInitializeEx
            module.COINIT_MULTITHREADED = 0
            module.CoInitializeEx = lambda flags: seen.append(("init", flags))

    monkeypatch.delitem(sys.modules, "comtypes", raising=False)
    monkeypatch.setattr(sys, "meta_path", [FakeComtypes(), *sys.meta_path])
    monkeypatch.setattr(sys, "coinit_flags", 2, raising=False)
    uia.init_com()
    assert seen == [("import", 0), ("init", 0)]
