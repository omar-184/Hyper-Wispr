from hyper_wispr.hotkeys import HANDS_FREE, IDLE, HotkeyStateMachine, key_matches


def make():
    events = []
    m = HotkeyStateMachine(
        ["ctrl_l", "cmd"],
        "space",
        on_start=lambda: events.append("start"),
        on_stop=lambda: events.append("stop"),
        on_cancel=lambda: events.append("cancel"),
    )
    return m, events


def test_key_matching():
    assert key_matches("ctrl", "ctrl_r")
    assert key_matches("cmd_l", "cmd")
    assert not key_matches("ctrl_l", "ctrl_r")


def test_push_to_talk():
    m, ev = make()
    m.key_down("ctrl_l")
    assert ev == []
    m.key_down("cmd")
    m.key_down("cmd")  # auto-repeat is ignored
    assert ev == ["start"]
    m.key_up("cmd")
    assert ev == ["start", "stop"] and m.state == IDLE


def test_hands_free_then_stop():
    m, ev = make()
    m.key_down("ctrl_l"), m.key_down("cmd"), m.key_down("space")
    assert m.state == HANDS_FREE
    m.key_up("space"), m.key_up("cmd"), m.key_up("ctrl_l")
    assert ev == ["start"]
    m.key_down("ctrl_l"), m.key_down("cmd")
    assert ev == ["start", "stop"] and m.state == IDLE


def test_escape_cancels():
    m, ev = make()
    m.key_down("ctrl_l"), m.key_down("cmd"), m.key_down("esc")
    assert ev == ["start", "cancel"] and m.state == IDLE
