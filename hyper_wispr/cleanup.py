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

# Some commands are also ordinary words, often in clinical notes: "sigmoid colon", "postoperative
# period", "a new line was placed", "next line of therapy". They stay words when the words around
# them show they are part of the sentence.
_DETERMINERS = {
    "a", "an", "the", "this", "that", "these", "those", "my", "your", "his", "her", "its", "our", "their",
    "of", "in", "into", "on", "to", "from", "within", "per", "each", "every", "any", "no", "same", "whole", "entire",
}
_WORD_BEFORE = {
    "colon": _DETERMINERS | {
        "sigmoid", "transverse", "ascending", "descending", "right", "left", "distal", "proximal", "redundant",
        "spastic", "irritable", "rectosigmoid", "dilated", "normal", "remaining", "residual",
    },
    "period": _DETERMINERS | {
        "menstrual", "postoperative", "post-operative", "postop", "post-op", "perioperative", "preoperative",
        "intraoperative", "postpartum", "peripartum", "perinatal", "neonatal", "postnatal", "incubation", "washout",
        "refractory", "latent", "induction", "grace", "waiting", "trial", "honeymoon", "gestational", "prodromal",
        "interim", "window", "time", "study", "last", "first", "short", "long", "brief",
    },
    "new line": _DETERMINERS,
    "next line": _DETERMINERS,
    "new paragraph": _DETERMINERS,
}
_WORD_AFTER = {
    "colon": {
        "cancer", "carcinoma", "polyp", "polyps", "mass", "wall", "mucosa", "resection", "lesion", "tumor", "tumour",
        "biopsy", "prep", "screening", "and", "or", "was", "is", "were",
    },
    "period": {
        "of", "was", "is", "were", "during", "when", "and", "or", "with", "after", "before", "following", "for",
        "from", "to", "in",
    },
    "new line": {"of", "placed", "placement", "was", "inserted"},
    "next line": {"of", "therapy", "treatment", "agent", "regimen"},
}
# "a six-week period", "over a 48 hour period"
_NUMBER_AND_UNIT = re.compile(
    r"(?:\d|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fourteen|twenty|thirty|forty|"
    r"sixty|ninety))[\s-]*(?:minute|hour|day|week|month|year)$"
)
_SENTENCE_END = ".!?:;\n"


def _is_unit_position(text: str, start: int, end: int) -> bool:
    """True for "5 mm" or "120/80 mm Hg": the word is a unit, not a hesitation."""
    return bool(re.search(r"\d\s*$", text[:start]) or re.match(r"\s*hg\b", text[end:], re.IGNORECASE))


def remove_fillers(text: str, fillers: list[str]) -> str:
    if not fillers:
        return text
    words = "|".join(re.escape(f) for f in fillers)
    pattern = re.compile(r"(?P<pre>,\s*)?\b(?P<w>" + words + r")\b(?P<post>[,.]?)\s*", re.IGNORECASE)

    def drop(m: re.Match[str]) -> str:
        word = m.group("w")
        # "ER", "MM": all capitals means an abbreviation, which Whisper never uses for a hesitation.
        if len(word) > 1 and word.isupper():
            return m.group(0)
        if _is_unit_position(m.string, m.start("w"), m.end("w")):
            return m.group(0)
        before = m.string[: m.start()].rstrip()
        if m.group("post") == "." and before and before[-1] not in _SENTENCE_END:
            return ". "  # "stable, um. Next" keeps its sentence end
        return " " if m.group("pre") and before else ""  # "the patient, um, is" -> "the patient is"

    return pattern.sub(drop, text)


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


def _reads_as_word(phrase: str, m: re.Match[str]) -> bool:
    """Should this spoken command stay a word because of the words around it?"""
    if phrase not in _WORD_BEFORE:
        return False
    text = m.string
    before = text[: m.start("word")].rstrip()
    sentence_start = not before or before[-1] in _SENTENCE_END
    prev = "" if sentence_start else before.split()[-1].strip("\"“”'‘’()[],").lower()
    after = re.match(r"\W*?([\w'’-]+)", text[m.end("word") :])
    nxt = after.group(1) if after else ""
    trail = m.group("trail")
    if prev in _WORD_BEFORE[phrase] or prev.endswith(("'s", "’s")):
        return True
    if phrase == "colon":
        # A sentence never starts with ":", so a "colon" there is the organ.
        return sentence_start or nxt.lower() in _WORD_AFTER["colon"]
    if phrase == "period":
        if _NUMBER_AND_UNIT.search(before.lower()) or nxt.lower() == "of":
            return True
        if not before:
            return True  # a dictation doesn't start with "."
        if trail == "." or (nxt[:1].isupper() and nxt != "I"):
            return False  # Whisper heard a sentence end here too
        return trail == "," or nxt.lower() in _WORD_AFTER["period"]
    return nxt.lower() in _WORD_AFTER.get(phrase, ())


def apply_spoken_punctuation(text: str) -> str:
    for phrase, symbol in SPOKEN_PUNCTUATION:
        pat = re.compile(r"[,.]?\s*\b(?P<word>" + phrase + r")\b(?P<trail>[,.]?)", re.IGNORECASE)

        def replace(m: re.Match[str], phrase: str = phrase, symbol: str = symbol) -> str:
            if _reads_as_word(phrase, m):
                return m.group(0)
            return symbol if symbol.startswith("\n") else symbol + " "

        text = pat.sub(replace, text)
    # Tidy spacing introduced above.
    text = re.sub(r"[ \t]+([" + re.escape(_NO_SPACE_BEFORE) + r"])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    return text


def apply_replacements(text: str, replacements: dict[str, str]) -> str:
    for src, dst in replacements.items():
        if not src.strip():
            continue
        # A function, not a template, so backslashes in the replacement (C:\Users) are kept as typed.
        text = re.sub(r"(?<!\w)" + re.escape(src) + r"(?!\w)", lambda _m, dst=dst: dst, text, flags=re.IGNORECASE)
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
        text = re.sub(r"[ \t]+([.,;:!?])", r"\1", text)
    # Punctuation first: "scratch that" removes back to the last sentence end, including a spoken "period".
    if cfg.get("spoken_punctuation", True):
        text = apply_spoken_punctuation(text)
    text = apply_backtrack(text, cfg.get("backtrack_phrases", []))
    text = apply_replacements(text, settings.get("replacements", {}))
    text = re.sub(r"[ \t]{2,}", " ", text).strip(" \t")
    return capitalize_sentences(text)


# ---- fitting into the text that's already there ----------------------------

# Words that are only capitalized because they start a sentence. Anything else (names, drugs,
# "I") keeps its capital when a dictation continues a sentence.
_COMMON_STARTS = set(
    "a an the and but or so then because if when while after before since until although though as of to in on at "
    "for with without from by about into over under between this that these those it its he she they we you his her "
    "their our your my there here which who whom whose what where why how is are was were be been has have had do "
    "does did will would can could should may might must not no also just only even still very more most less some "
    "any all each every both either neither other another such than too again once please thanks".split()
)
_OPENERS = "([{\"'“‘/-"


def fit_to_context(text: str, before: str, after: str = "") -> tuple[str, bool]:
    """Make a dictation continue the text around the cursor. Returns (text, whether a space
    should follow it). before/after: the text just before and after the cursor ("" = unknown)."""
    if not text:
        return text, space_after(after)
    if before and not before[-1].isspace() and before[-1] not in _OPENERS and text[0] not in _NO_SPACE_BEFORE:
        text = " " + text  # "the patient" + "is stable" -> "the patient is stable"
    tail = before.rstrip(" \t")
    if tail and tail[-1] not in ".!?:\n" and not tail.endswith(("...", "…")):
        # Mid-sentence: "The" becomes "the", but names, drugs and "I" keep their capital.
        m = re.match(r"(\s*)([A-Z][a-z']*)\b", text)
        if m and m.group(2).lower() in _COMMON_STARTS:
            text = text[: m.start(2)] + m.group(2).lower() + text[m.end(2):]
    add_space = space_after(after)
    if after and after[0] in ".,;:!?" and text.endswith("."):
        text = text[:-1]  # inserting into a sentence: don't end it early
    return text, add_space


def space_after(after: str) -> bool:
    """Should a space follow the dictation? Not when there's already one (or punctuation) after the cursor."""
    return not (after and (after[0].isspace() or after[0] in _NO_SPACE_BEFORE))


def apply_style(text: str, style: str) -> str:
    """The parts of a tone the rules can do on their own (the AI step does the rest)."""
    if style == "very_casual":
        text = re.sub(r"^(\s*)([A-Z][a-z']*)\b", lambda m: m.group(1) + (m.group(2).lower() if m.group(2).lower() in _COMMON_STARTS else m.group(2)), text)
        if text.endswith(".") and not text.endswith(".."):
            text = text[:-1]
    elif style == "casual" and text.endswith(".") and not re.search(r"[.!?]\s", text):
        text = text[:-1]  # a one-sentence chat message reads more naturally without the period
    return text
