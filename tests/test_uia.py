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


class _Ranges:
    def __init__(self, *ranges):
        self.ranges = ranges
        self.Length = len(ranges)

    def GetElement(self, i):
        return self.ranges[i]


class _Element:
    def __init__(self, patterns):
        self.patterns = patterns

    def GetCurrentPattern(self, pattern_id):
        found = self.patterns.get(pattern_id)
        return type("Unknown", (), {"QueryInterface": lambda self, interface: found})() if found else None


def test_a_bare_cursor_is_found_when_the_text_box_gives_no_selection():
    """Some text boxes answer "no selection" when nothing is selected, so the text before the
    cursor was never read; the cursor's own range is asked for then (UI Automation's TextPattern2)."""
    mod = type("Mod", (), {"IUIAutomationTextPattern2": object})
    selected, caret = object(), object()
    text = type("Text", (), {"GetSelection": lambda self: _Ranges(selected)})()
    assert uia._selection(_Element({}), text, mod) is selected

    bare = type("Text", (), {"GetSelection": lambda self: _Ranges()})()
    text2 = type("Text2", (), {"GetCaretRange": lambda self: (True, caret)})()
    assert uia._selection(_Element({uia.TEXT_PATTERN2: text2}), bare, mod) is caret
    assert uia._selection(_Element({}), bare, mod) is None  # no cursor to ask for: don't know
    assert uia._selection(_Element({uia.TEXT_PATTERN2: text2}), bare, type("Old", (), {})) is None
