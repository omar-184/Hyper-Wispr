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


class _User32:
    """The two things asked of a classic Windows text box: its kind, and where its cursor is."""

    def __init__(self, kind="Edit", selection=(0, 0), length=0):
        self.kind, self.selection, self.length = kind, selection, length

    def RealGetWindowClassW(self, hwnd, name, size):
        name.value = self.kind
        return len(self.kind)

    def SendMessageTimeoutW(self, hwnd, msg, wparam, lparam, flags, timeout, result):
        import ctypes

        if msg == uia.WM_GETTEXTLENGTH:
            result._obj.value = self.length
        elif msg == uia.EM_GETSEL:
            ctypes.c_uint32.from_address(wparam).value, ctypes.c_uint32.from_address(lparam).value = self.selection
        return 1


def _classic_box(text):
    value = type("Value", (), {"CurrentValue": text, "CurrentIsReadOnly": False})()
    element = _Element({uia.VALUE_PATTERN: value})
    element.CurrentNativeWindowHandle, element.CurrentIsPassword, element.CurrentControlType = 1234, False, uia.EDIT
    return element, value


def test_a_classic_text_box_is_asked_where_its_cursor_is(monkeypatch):
    """Classic Windows text boxes (Notepad before Windows 11, many older programs) give UI Automation
    their whole text but not where the cursor is: measured on Windows, the text before the cursor
    always read as empty there, so a dictation couldn't continue the sentence."""
    text = "Ward round. Overnight the patient is stable"
    element, value = _classic_box(text)
    cursor = text.index(" is stable")
    monkeypatch.setattr(uia, "_user32", lambda: _User32(selection=(cursor, cursor), length=len(text)))
    assert uia._classic_edit(element, value, 600, 200) == ("Ward round. Overnight the patient", "", " is stable")
    assert uia._classic_edit(element, value, 11, 3) == ("the patient", "", " is")

    start = text.index("Overnight")
    monkeypatch.setattr(uia, "_user32", lambda: _User32(selection=(start, cursor), length=len(text)))
    assert uia._classic_edit(element, value, 600, 200) == ("Ward round. ", "Overnight the patient", " is stable")

    # The box counts in UTF-16 units, where an emoji takes two.
    element, value = _classic_box("😀 ok then")
    monkeypatch.setattr(uia, "_user32", lambda: _User32(selection=(5, 5), length=10))
    assert uia._classic_edit(element, value, 600, 200) == ("😀 ok", "", " then")

    # Anything else is "don't know": another kind of box, a huge one, or an answer that doesn't fit.
    for box in (_User32(kind="ComboBox", length=10), _User32(length=uia.EDIT_MAX_CHARS + 1), _User32(selection=(3, 99), length=10)):
        monkeypatch.setattr(uia, "_user32", lambda box=box: box)
        assert uia._classic_edit(element, value, 600, 200) is None


def test_the_text_around_the_cursor_is_read_from_a_classic_text_box(monkeypatch):
    text = "Overnight the patient"
    element, _value = _classic_box(text)
    fake = type("UIA", (), {"GetFocusedElement": lambda self: element})()
    mod = type("Mod", (), {"IUIAutomationTextPattern": object, "IUIAutomationValuePattern": object})
    monkeypatch.setattr(uia, "_uia", lambda: (fake, mod))
    monkeypatch.setattr(uia, "_user32", lambda: _User32(selection=(len(text), len(text)), length=len(text)))
    focus = uia._read_focus(600, 200)
    assert (focus.known, focus.editable, focus.before, focus.after) == (True, True, text, "")

    element.CurrentIsPassword = True
    assert uia._read_focus(600, 200).before == ""  # never a password box
