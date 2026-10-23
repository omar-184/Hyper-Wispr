from hyper_wispr.hotkeys import HANDS_FREE, HOLDING, IDLE, HotkeyStateMachine, key_matches


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make():
    events, clock = [], Clock()
    m = HotkeyStateMachine(
        ["ctrl_l", "cmd"],
        0.35,
        on_start=lambda: events.append("start"),
        on_stop=lambda: events.append("stop"),
        on_cancel=lambda: events.append("cancel"),
        clock=clock,
    )
    return m, events, clock


def press(m, *keys):
    for k in keys:
        m.key_down(k)


def release(m, *keys):
    for k in keys:
        m.key_up(k)


def test_key_matching():
    assert key_matches("ctrl", "ctrl_r")
    assert key_matches("cmd_l", "cmd")
    assert not key_matches("ctrl_l", "ctrl_r")


def test_push_to_talk():
    m, ev, clock = make()
    m.key_down("ctrl_l")
    assert ev == []
    m.key_down("cmd")
    m.key_down("cmd")  # auto-repeat is ignored
    assert ev == ["start"] and m.state == HOLDING
    clock.t = 2.0
    m.key_up("cmd")
    assert ev == ["start", "stop"] and m.state == IDLE
    m.key_up("ctrl_l")
    assert ev == ["start", "stop"]


def test_tap_starts_hands_free_and_tap_stops():
    m, ev, clock = make()
    press(m, "ctrl_l", "cmd")
    clock.t = 0.2
    release(m, "cmd", "ctrl_l")
    assert ev == ["start"] and m.state == HANDS_FREE
    clock.t = 10.0
    press(m, "ctrl_l", "cmd")
    assert ev == ["start", "stop"]
    clock.t = 10.1
    release(m, "cmd", "ctrl_l")
    assert ev == ["start", "stop"] and m.state == IDLE
    # And the next hold works normally.
    press(m, "ctrl_l", "cmd")
    assert ev == ["start", "stop", "start"]


def test_other_keys_while_holding_do_nothing():
    m, ev, clock = make()
    press(m, "ctrl_l", "cmd", "space")
    release(m, "space")
    assert ev == ["start"] and m.state == HOLDING


def test_escape_cancels():
    m, ev, _ = make()
    press(m, "ctrl_l", "cmd", "esc")
    assert ev == ["start", "cancel"] and m.state == IDLE


def test_escape_when_idle_does_nothing():
    m, ev, _ = make()
    press(m, "esc")
    assert ev == []


def test_recovers_from_missed_key_up():
    m, ev, clock = make()
    press(m, "ctrl_l", "cmd")
    clock.t = 0.1
    release(m, "cmd", "ctrl_l")  # hands-free
    press(m, "ctrl_l", "cmd")  # stop
    m.pressed.clear()  # key-ups lost (e.g. Win+L)
    press(m, "ctrl_l", "cmd")
    assert ev == ["start", "stop", "start"] and m.state == HOLDING


def test_reset():
    m, ev, _ = make()
    press(m, "ctrl_l", "cmd")
    m.reset()
    assert m.state == IDLE
