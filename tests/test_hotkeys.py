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


def make_with_keyboard(physically_down):
    """A machine that can ask "the keyboard" which keys are really down, like on Windows."""
    events, clock = [], Clock()
    m = HotkeyStateMachine(
        ["ctrl_l", "cmd"],
        0.35,
        on_start=lambda: events.append("start"),
        on_stop=lambda: events.append("stop"),
        on_cancel=lambda: events.append("cancel"),
        clock=clock,
        is_down=lambda name: name in physically_down,
        on_combo=lambda: events.append("combo"),
    )
    return m, events, clock


def test_stuck_win_key_does_not_turn_ctrl_into_the_shortcut():
    down = set()
    m, ev, _ = make_with_keyboard(down)
    down.add("cmd")
    press(m, "cmd")
    press(m, "l")  # Win+L locks the screen; Windows never sends the key-ups
    down.clear()
    down.add("ctrl_l")
    press(m, "ctrl_l")  # later, Ctrl+C
    assert ev == [] and m.state == IDLE
    assert m.pressed == {"ctrl_l"}


def test_missed_release_while_holding_finishes_the_dictation():
    down = {"ctrl_l", "cmd"}
    m, ev, clock = make_with_keyboard(down)
    press(m, "ctrl_l", "cmd")
    assert ev == ["combo", "start"] and m.state == HOLDING
    clock.t = 5.0
    down.clear()  # let go while an administrator window had the focus
    press(m, "a")
    assert ev == ["combo", "start", "stop"] and m.state == IDLE


def test_held_keys_are_not_dropped_while_holding():
    down = {"ctrl_l", "cmd"}
    m, ev, _ = make_with_keyboard(down)
    press(m, "ctrl_l", "cmd")
    down.add("space")
    press(m, "space")
    assert ev == ["combo", "start"] and m.state == HOLDING


def test_combo_callback_fires_for_start_and_hands_free_stop():
    down = {"ctrl_l", "cmd"}
    m, ev, clock = make_with_keyboard(down)
    press(m, "ctrl_l", "cmd")
    clock.t = 0.1
    release(m, "cmd", "ctrl_l")  # tap: hands-free
    clock.t = 5.0
    press(m, "ctrl_l", "cmd")  # stop: Win is down again, so the Start menu needs masking again
    assert ev == ["combo", "start", "combo", "stop"]


def test_which_shortcuts_need_the_menu_mask():
    from hyper_wispr.hotkeys import uses_menu_key

    assert uses_menu_key(["ctrl_l", "cmd"])
    assert uses_menu_key(["alt_gr", "space"])
    assert not uses_menu_key(["ctrl_l", "shift"])
    assert not uses_menu_key(["f8"])
