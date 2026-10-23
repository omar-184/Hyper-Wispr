from hyper_wispr import overlay


def test_level_scale():
    assert overlay.level_to_unit(0.0) == 0.0
    assert overlay.level_to_unit(1.0) == 1.0
    assert 0.2 < overlay.level_to_unit(0.02) < 0.8  # normal speech lands mid-scale


def test_pill_renders_every_state_and_scales():
    for state in (overlay.RECORDING, overlay.HANDS_FREE, overlay.WORKING):
        small = overlay.render_pill(state, 0.5, 0.6, 1.0)
        big = overlay.render_pill(state, 0.5, 0.6, 2.0)
        assert small.mode == "RGBA"
        assert abs(big.height - 2 * small.height) <= 2
        assert small.getpixel((small.width // 2, small.height // 2))[3] > 200  # solid middle
        assert small.getpixel((0, 0))[3] < 10  # transparent corner


def test_disabled_overlay_stays_hidden():
    ov = overlay.Overlay(level=lambda: 0.0, enabled=False)
    ov.show(overlay.RECORDING)
    assert ov._state == overlay.HIDDEN
