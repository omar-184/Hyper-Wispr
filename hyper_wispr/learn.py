"""Learn words from your corrections.

After a dictation is pasted, Hyper-Wispr keeps an eye on that text box for up to a minute. If
you then fix a word of the dictation by retyping it as a similar-looking word (say "Farghal"
where it wrote "Fargal"), that word is offered for your dictionary in Settings → Vocabulary.
Nothing is added without you saying so.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from difflib import SequenceMatcher
from typing import Callable

from . import uia

log = logging.getLogger(__name__)

_WORD = re.compile(r"[^\W\d_](?:[\w'’-]*\w)?")
WATCH_SECONDS = 60
POLL_SECONDS = 3


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def corrected_words(pasted: str, now: str, known: set[str]) -> list[tuple[str, str]]:
    """(as dictated, as corrected) for each single word of `pasted` that was replaced in `now`
    by a similar-looking word. Only replacements with unchanged words on both sides count, so
    rewording a sentence isn't mistaken for a spelling correction."""
    was, new = _WORD.findall(pasted), _WORD.findall(now)
    if len(was) < 2 or not new:
        return []
    matcher = SequenceMatcher(None, [w.lower() for w in was], [w.lower() for w in new], autojunk=False)
    ops = matcher.get_opcodes()
    known_lower = {k.lower() for k in known}
    found = []
    for i, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag != "replace" or i2 - i1 != 1 or j2 - j1 != 1:
            continue
        before_ok = i > 0 and ops[i - 1][0] == "equal"
        after_ok = i + 1 < len(ops) and ops[i + 1][0] == "equal"
        if not (before_ok and (after_ok or i2 == len(was))):
            continue
        heard, fixed = was[i1], new[j1]
        if len(fixed) < 3 or fixed.lower() in known_lower or _similar(heard, fixed) < 0.5:
            continue
        found.append((heard, fixed))
    return found


class Learner:
    """Watches the text box of the last dictation, in the background."""

    def __init__(self, on_word: Callable[[str, str], None], known: Callable[[], set[str]]):
        self.on_word = on_word
        self.known = known
        self.jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True, name="learn").start()

    def watch(self, pasted: str) -> None:
        """Call right after pasting, while the text box still has the focus."""
        handle = uia.remember_focus()
        if handle is not None:
            self.jobs.put((handle, pasted))

    def _run(self) -> None:
        while True:
            handle, pasted = self.jobs.get()
            try:
                self._follow(handle, pasted)
            except Exception:
                log.debug("Learning from edits failed", exc_info=True)
            finally:
                uia.forget(handle)

    def _follow(self, handle: int, pasted: str) -> None:
        latest = None
        deadline = time.monotonic() + WATCH_SECONDS
        while time.monotonic() < deadline and self.jobs.empty():
            time.sleep(POLL_SECONDS)
            text = uia.text_of(handle)
            if text is not None:
                latest = text
            if not uia.has_focus(handle):
                break  # you moved on to something else
        if latest is None:
            return
        for heard, fixed in corrected_words(pasted, latest, self.known()):
            log.info("Learned a corrected word (offered for the dictionary)")
            self.on_word(fixed, heard)
