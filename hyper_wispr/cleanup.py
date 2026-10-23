"""Fast rule-based cleanup that runs on every dictation, before the optional AI step."""

from __future__ import annotations

import re
from typing import Any

# Spoken phrase -> text. Order matters: longer phrases first.
SPOKEN_PUNCTUATION: list[tuple[str, str]] = [
    ("new paragraph", "\n\n"),
    ("new line", "\n"),
    ("next line", "\n"),
    ("full stop", "."),
    ("question mark", "?"),
    ("exclamation mark", "!"),
    ("exclamation point", "!"),
    ("open bracket", " ("),
    ("close bracket", ")"),
    ("open parenthesis", " ("),
    ("close parenthesis", ")"),
    ("semicolon", ";"),
    ("colon", ":"),
    ("comma", ","),
    ("period", "."),
]

_NO_SPACE_BEFORE = ".,;:!?)"


def remove_fillers(text: str, fillers: list[str]) -> str:
    if not fillers:
        return text
    pattern = r"\b(?:" + "|".join(re.escape(f) for f in fillers) + r")\b[,.]?\s*"
    return re.sub(pattern, "", text, flags=re.IGNORECASE)


def apply_backtrack(text: str, phrases: list[str]) -> str:
    """'I'll be there at 5 scratch that at 6' -> 'at 6'.

    Deletes the sentence (or clause after the last sentence break) spoken before the phrase.
    """
    for phrase in phrases:
        pat = re.compile(r"[,.]?\s*\b" + re.escape(phrase) + r"\b[,.]?\s*", re.IGNORECASE)
        while (m := pat.search(text)) is not None:
            before, after = text[: m.start()], text[m.end() :]
            cut = max(before.rfind(". "), before.rfind("? "), before.rfind("! "), before.rfind("\n"))
            before = before[: cut + 1] + " " if cut >= 0 else ""
            text = (before + after).strip()
    return text


def apply_spoken_punctuation(text: str) -> str:
    for phrase, symbol in SPOKEN_PUNCTUATION:
        pat = re.compile(r"[,.]?\s*\b" + phrase + r"\b[,.]?", re.IGNORECASE)
        text = pat.sub(lambda _m, s=symbol: s if s.startswith("\n") else s + " ", text)
    # Tidy spacing introduced above.
    text = re.sub(r"[ \t]+([" + re.escape(_NO_SPACE_BEFORE) + r"])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    return text


def apply_replacements(text: str, replacements: dict[str, str]) -> str:
    for src, dst in replacements.items():
        text = re.sub(r"\b" + re.escape(src) + r"\b", dst, text, flags=re.IGNORECASE)
    return text


def match_snippet(text: str, snippets: dict[str, str]) -> str | None:
    key = re.sub(r"[^\w\s]", "", text).strip().lower()
    for trigger, expansion in snippets.items():
        if key == trigger.strip().lower():
            return expansion
    return None


def capitalize_sentences(text: str) -> str:
    def up(m: re.Match[str]) -> str:
        return m.group(1) + m.group(2).upper()

    text = re.sub(r"(^\s*)([a-z])", up, text)
    return re.sub(r"([.!?]\s+|\n+)([a-z])", up, text)


def clean(text: str, settings: dict[str, Any]) -> str:
    cfg = settings["cleanup"]
    text = text.strip()
    if cfg.get("remove_fillers", True):
        text = remove_fillers(text, cfg.get("filler_words", []))
    text = apply_backtrack(text, cfg.get("backtrack_phrases", []))
    if cfg.get("spoken_punctuation", True):
        text = apply_spoken_punctuation(text)
    text = apply_replacements(text, settings.get("replacements", {}))
    text = re.sub(r"[ \t]{2,}", " ", text).strip(" \t")
    return capitalize_sentences(text)
