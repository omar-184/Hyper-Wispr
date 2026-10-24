"""Entry point: `python -m hyper_wispr` (or the `hyper-wispr` command)."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__, config, context


def main() -> None:
    parser = argparse.ArgumentParser(prog="hyper-wispr", description="Offline voice dictation for Windows.")
    parser.add_argument("--transcribe", metavar="AUDIO_FILE", help="transcribe a file and print the cleaned text, then exit")
    parser.add_argument("--app-type", default=context.OTHER, help="writing style to use with --transcribe")
    parser.add_argument("--self-test", action="store_true", help="check that all components load, then exit (used by the build)")
    parser.add_argument("--minimized", action="store_true", help="start in the tray without opening the window (used at login)")
    parser.add_argument("--settings", action="store_true", help="open the Hyper-Wispr window (the tray app does this for you)")
    parser.add_argument("--settings-self-test", action="store_true", help="open the Settings window, check it works, close it (used by the build)")
    parser.add_argument("--overlay-self-test", action="store_true", help="flash the listening indicator, then exit (used by the build)")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()

    log_path = config.app_dir() / "hyper-wispr.log"
    if not (args.settings or args.settings_self_test):
        trim_log(log_path)
    handlers: list[logging.Handler] = [logging.FileHandler(log_path, encoding="utf-8")]
    if sys.stdout is not None:  # None when started without a console (pythonw)
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)

    if args.self_test:
        sys.exit(self_test())

    if args.overlay_self_test:
        from .overlay import self_test as overlay_self_test

        sys.exit(overlay_self_test())

    if args.settings or args.settings_self_test:
        # Runs next to the tray app, so it must not take the single-instance lock below.
        sys.exit(open_settings_window(self_test=args.settings_self_test))

    if args.transcribe:
        from .app import process_text
        from .speech import Transcriber

        settings = config.load()
        t = Transcriber(settings)
        raw = t.transcribe(args.transcribe)
        print("RAW:  ", raw)
        print("FINAL:", process_text(raw, settings, args.app_type))
        return

    if not acquire_single_instance():
        # Starting it again from the Start menu or desktop means "show me Hyper-Wispr".
        logging.info("Already running; showing the window instead")
        sys.exit(0 if args.minimized else open_settings_window())

    from .app import App

    App(open_window=not args.minimized).run()


LOG_LIMIT = 5_000_000  # bytes


def trim_log(path, limit: int = LOG_LIMIT) -> None:
    """Keep the log from growing forever: past the limit it becomes hyper-wispr.old.log (replacing
    the previous one) and a new log starts. Done at start-up, before anything has the file open;
    a rotating log handler can't be used because the Settings window writes to the same file."""
    try:
        if path.stat().st_size > limit:
            path.replace(path.with_name("hyper-wispr.old.log"))
    except OSError:
        pass  # no log yet, or the Settings window has it open: try again next start


_instance_lock = None


def acquire_single_instance() -> bool:
    """Only one tray app may run: two copies both record and both paste every dictation."""
    global _instance_lock
    if sys.platform != "win32":
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _instance_lock = kernel32.CreateMutexW(None, False, "Local\\HyperWisprTrayApp")
    ERROR_ALREADY_EXISTS = 183
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def open_settings_window(self_test: bool = False) -> int:
    try:
        from .settings_ui import run

        return run(self_test=self_test)
    except Exception:
        # Without a console the traceback would vanish; the tray app reads the exit code.
        logging.exception("The Settings window failed")
        return 1


def self_test() -> int:
    """Import every heavy dependency so a broken installer build fails in CI, not on a user's PC."""
    import numpy as np

    try:
        import ctranslate2
        import faster_whisper  # noqa: F401
        import pynput.keyboard  # noqa: F401
        import pystray  # noqa: F401
        import sounddevice  # noqa: F401
        import webview  # noqa: F401
        from faster_whisper.vad import get_speech_timestamps

        from . import app  # noqa: F401
        from .speech import cuda_available

        get_speech_timestamps(np.zeros(16000, dtype=np.float32))
        if sys.platform == "win32":
            import pycaw.pycaw  # noqa: F401

            from . import secure, uia

            if not uia.self_test():
                raise RuntimeError("UI Automation could not be loaded")
            if secure.decrypt(secure.encrypt(b"test")) != b"test":
                raise RuntimeError("Encryption round trip failed")
        logging.info("Self-test OK: ctranslate2 %s, CUDA devices: %s", ctranslate2.__version__, cuda_available())
        return 0
    except Exception:
        logging.exception("Self-test failed")
        return 1


if __name__ == "__main__":
    main()
