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
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()

    handlers: list[logging.Handler] = [logging.FileHandler(config.app_dir() / "hyper-wispr.log", encoding="utf-8")]
    if sys.stdout is not None:  # None when started without a console (pythonw)
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)

    if args.transcribe:
        from .app import process_text
        from .speech import Transcriber

        settings = config.load()
        t = Transcriber(settings)
        raw = t.transcribe(args.transcribe)
        print("RAW:  ", raw)
        print("FINAL:", process_text(raw, settings, args.app_type))
        return

    from .app import App

    App().run()


if __name__ == "__main__":
    main()
