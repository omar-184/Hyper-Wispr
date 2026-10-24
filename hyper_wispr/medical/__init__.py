"""Medical dictation: a medical dictionary that helps every step of a dictation.

The dictionary is made of word lists ("packs") in data/: drugs, anatomy, conditions, abbreviations,
optional specialty packs, and the FDA drug-name lists. You can add your own words in the Medical
page, hide built-in ones, or drop your own .txt lists into %APPDATA%\\HyperWispr\\medical.

While medical dictation is on, the dictionary is used three ways, all on this computer:
1. The speech model gets the most important terms as a hint, so it hears them right.
2. Spellings that are close to a medical term ("metoprolal", "levo floxacin") are corrected, and
   spoken units are written the standard way ("25 milligrams" -> "25 mg").
3. The AI cleanup gets the medical writing rules and the terms that sound like what was said.
"""

from __future__ import annotations

import gzip
import logging
import re
import threading
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"

# Specialty packs, in the order the Medical page shows them.
SPECIALTIES = ["neuro", "cardio", "icu", "surgery", "obgyn", "peds"]
# Whisper keeps only the last 223 tokens of its prompt; leave room for the words around them.
PROMPT_TOKENS = 200
# Built-in hints given to the speech model at most, so your own words always fit.
MAX_PACK_HINTS = 30


@dataclass
class Pack:
    id: str
    title: str
    description: str = ""
    source: str = ""
    kind: str = "core"  # core | specialty | fda | user
    terms: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)  # the *-marked terms
    aliases: dict[str, str] = field(default_factory=dict)  # heard (lowercase) -> term


def parse(text: str, pack_id: str, kind: str) -> Pack:
    pack = Pack(id=pack_id, title=pack_id.replace("-", " ").capitalize(), kind=kind)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = re.match(r"#\s*(Title|Description|Source):\s*(.+)", line)
            if m:
                setattr(pack, m.group(1).lower(), m.group(2).strip())
            continue
        star = line.startswith("*")
        term, _, heard = line.lstrip("*").partition("<-")
        term = term.strip()
        if not term:
            continue
        pack.terms.append(term)
        if star:
            pack.hints.append(term)
        for alias in heard.split(";"):
            if alias.strip():
                pack.aliases[alias.strip().lower()] = term
    return pack


def _kind(stem: str) -> str:
    return stem.split("-")[0] if stem.split("-")[0] in ("core", "specialty", "fda") else "core"


_builtin: dict[str, Pack] | None = None
_builtin_lock = threading.Lock()


def builtin_packs() -> dict[str, Pack]:
    """id -> Pack for the lists shipped with the app ("neuro" for specialty-neuro.txt)."""
    global _builtin
    with _builtin_lock:
        if _builtin is None:
            packs = {}
            for path in sorted(DATA_DIR.glob("*.txt")):
                if path.stem.startswith("english"):
                    continue
                kind = _kind(path.stem)
                pack_id = path.stem.removeprefix("specialty-")
                packs[pack_id] = parse(path.read_text(encoding="utf-8"), pack_id, kind)
            _builtin = packs
        return _builtin


def user_dir() -> Path:
    from .. import config

    return config.app_dir() / "medical"


_user_cache: tuple[tuple, list[Pack]] = ((), [])


def user_packs() -> list[Pack]:
    """Your own word lists: every .txt file in %APPDATA%\\HyperWispr\\medical."""
    global _user_cache
    try:
        files = sorted(p for p in user_dir().glob("*.txt") if p.stem.lower() != "readme")
        key = tuple((p.name, p.stat().st_mtime, p.stat().st_size) for p in files)
    except OSError:
        return []
    if key != _user_cache[0]:
        packs = []
        for p in files:
            try:
                # utf-8-sig: Notepad may save a byte-order mark.
                packs.append(parse(p.read_text(encoding="utf-8-sig", errors="replace"), "user:" + p.stem, "user"))
                packs[-1].title = p.stem
            except OSError:
                log.warning("Could not read the word list %s", p.name)
        _user_cache = (key, packs)
    return _user_cache[1]


# ---- settings ---------------------------------------------------------------

def cfg(settings: dict[str, Any]) -> dict[str, Any]:
    return settings.get("medical") or {}


def enabled(settings: dict[str, Any]) -> bool:
    return bool(cfg(settings).get("enabled"))


def active_packs(settings: dict[str, Any]) -> list[Pack]:
    c = cfg(settings)
    chosen = set(c.get("specialties") or [])
    out = []
    for pack in builtin_packs().values():
        if pack.kind == "core" or (pack.kind == "specialty" and pack.id in chosen) or (pack.kind == "fda" and c.get("fda_drug_names", True)):
            out.append(pack)
    return out + user_packs()


# ---- the word index ---------------------------------------------------------

_LETTERS = re.compile(r"[a-z]")
# Sounds a speech model mixes up at the start of a word: compare those within one group.
_FIRST_GROUPS = {**dict.fromkeys("aeiouy", "a"), **dict.fromkeys("ckqsxz", "k"), **dict.fromkeys("fp", "f"),
                 **dict.fromkeys("gj", "g"), **dict.fromkeys("dt", "d"), **dict.fromkeys("mn", "m")}


def _group(word: str) -> str:
    return _FIRST_GROUPS.get(word[0], word[0])


def _sounds(word: str) -> str:
    """A rough spelling-independent form: "ceftriaxone" and "seftriaxon" come out alike."""
    w = word.lower()
    for a, b in (("ph", "f"), ("ck", "k"), ("qu", "kw"), ("x", "ks"), ("y", "i"), ("ae", "e"), ("oe", "e")):
        w = w.replace(a, b)
    w = re.sub(r"c(?=[eiy])", "s", w)
    w = w.replace("c", "k")
    w = re.sub(r"(.)\1+", r"\1", w)  # double letters
    w = re.sub(r"(?<=.)[aeiou]+$", "", w)  # final vowels are often dropped or added
    return w


# Word parts that carry meaning. A word that differs from a term in one of them is another word, not a
# slip: "hypermagnesemia" isn't "hypomagnesemia", "tachycardic" isn't "tachycardia", "thrombolysis"
# isn't "thrombosis".
_LEADS = sorted("""hyper hypo intra extra inter infra supra sub pre post peri ante endo exo ecto epi para retro trans
    brady tachy micro macro mono poly oligo pan hemi anti dys myo myelo neur nephr hepat gastr enter cyst oste arthr
    chondr cardi angi arter ven hemat lymph leuk erythr thromb cyt pneum bronch trache laryng pharyng rhin ophthalm
    crani encephal cerebr spondyl thyr adren pancreat proct ureter urethr pyel men metr mening col colp salping oophor
    orch prostat mast derm dermat hyster lapar thorac pleur""".split(), key=len, reverse=True)
_ENDINGS = sorted("""itis itic osis otic emia emic uria uric pnea pneic penia penic rrhagia rrhagic rrhea ectomy otomy
    ostomy plasty plastic plasia pexy rrhaphy scopy scope scopic graphy gram graph lysis lytic pathy pathic algia
    megaly cytosis kinesia kinesis kinetic trophy trophic ia ic al ar ary ous ive ion ed er ing y a""".split(),
    key=len, reverse=True)


def _lead(word: str) -> str:
    return next((p for p in _LEADS if word.startswith(p) and len(word) - len(p) >= 3), "")


def _ending(word: str) -> str:
    return next((e for e in _ENDINGS if word.endswith(e) and len(word) - len(e) >= 3), "")


def _consonants(text: str) -> str:
    return re.sub(r"[aeiouy]", "", text)


def changes_meaning(word: str, term: str) -> bool:
    """True if `term` differs from `word` in a part that carries meaning, so it can't be a slip of it.
    A vowel slip inside such a part ("pancroatitis", "gallbladdar") is still a slip."""
    w, t = word.lower(), term.lower()
    lw, lt = _lead(w), _lead(t)
    if lw and lt and not (lw.startswith(lt) or lt.startswith(lw)):
        return True  # hyper/hypo, intra/extra, arter/arthr
    if lw and not lt and len(lw) >= 4 and lw not in ("ante", "para", "post", "poly") and not t.startswith(lw):
        return True  # a root the term doesn't have: thyroxine isn't tyrosine
    ew, et = _ending(w), _ending(t)
    if ew != et:
        if len(ew) >= 4 and _consonants(ew) != _consonants(t[-len(ew):]):
            return True  # "-itis" isn't "-osis", nor "epididymis" for "epididymitis"
        if ew and et and _consonants(ew) != _consonants(et):
            if w[: -len(ew)].rstrip("aeiou") == t[: -len(et)].rstrip("aeiou"):
                return True  # the same stem with another ending: tachycardic, nebulized
    return False


def _edits(a: str, b: str) -> int:
    """Letters to add, drop or change to turn a into b."""
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


class Lexicon:
    """The words of the active packs, indexed for fast "is there a term that looks like this"."""

    def __init__(self, packs: Iterable[Pack], extra: Iterable[str] = (), hidden: Iterable[str] = ()):
        hidden_l = {h.strip().lower() for h in hidden if h.strip()}
        self.terms: dict[str, str] = {}  # lowercase -> as written
        self.source: dict[str, str] = {}  # lowercase -> pack title
        self.aliases: dict[str, str] = {}
        self.packs = list(packs)
        for pack in self.packs:
            for t in pack.terms:
                if t.lower() not in hidden_l:
                    self.terms.setdefault(t.lower(), t)
                    self.source.setdefault(t.lower(), pack.title)
            for heard, t in pack.aliases.items():
                if t.lower() not in hidden_l:
                    self.aliases[heard] = t
        for t in extra:
            t = t.strip()
            if t:
                self.terms[t.lower()] = t
                self.source[t.lower()] = "Your words"
        # Words of multi-word terms ("fibrillation" of "atrial fibrillation") count as medical words too.
        self.parts: dict[str, str] = {}
        for low, term in self.terms.items():
            if " " in low:
                fda = self.source[low].startswith("FDA")
                for part, written in zip(low.split(), term.split()):
                    # Not the packaging words of brand names ("Compressed", "Sprinkle")
                    if len(part) >= 5 and part not in self.terms and not (fda and _is_english(part)):
                        self.parts.setdefault(part, written)
        # Single words long enough to compare safely, bucketed by how they start.
        self.words: dict[str, list[tuple[str, str, str]]] = {}
        for low, term in list(self.terms.items()) + list(self.parts.items()):
            if " " in low or len(low) < 5 or not low.isascii() or not _LETTERS.match(low):
                continue
            key = re.sub(r"[^a-z]", "", low)
            self.words.setdefault(_group(key), []).append((key, term, _sounds(key)))

    def knows(self, word: str) -> bool:
        low = word.lower()
        return low in self.terms or low in self.parts

    def candidates(self, word: str) -> list[tuple[str, str, str]]:
        return self.words.get(_group(word), [])

    def best(self, word: str, threshold: float) -> tuple[str, float] | None:
        """The one term that looks like `word` (ratio >= threshold), or None if there's none, two look
        about as alike, or the closest means something else ("hypermagnesemia" is never "hypomagnesemia")."""
        w = word.lower()
        ws = _sounds(w)
        scored = []
        for key, term, sounds in self.candidates(w):
            if abs(len(key) - len(w)) > max(2, len(w) // 4):
                continue
            m = SequenceMatcher(None, w, key)
            if m.real_quick_ratio() < threshold - 0.1 or m.quick_ratio() < threshold - 0.1:
                continue
            score = m.ratio()
            if score < threshold - 0.2:
                continue  # sounding alike only counts for words that are spelled alike too ("Qassim")
            score = max(score, SequenceMatcher(None, ws, sounds).ratio() - 0.02)
            score -= 0.03 * max(0, abs(len(key) - len(w)) - 1)  # "testlist" isn't "testis"
            if score >= threshold:
                scored.append((score, term, key))
        if not scored:
            return None
        scored.sort(reverse=True)
        score, term, key = scored[0]
        if changes_meaning(w, key):
            return None
        for other_score, other, other_key in scored[1:]:
            if other.lower() == term.lower():
                continue
            if other_score > score - 0.04:
                return None  # two terms about as close: leave it alone
            # One slip from either of two terms that mean different things: "hypetension", "laminoctomy".
            apart = changes_meaning(key, other_key) or changes_meaning(other_key, key)
            if apart and _edits(w, other_key) <= _edits(w, key):
                return None
        return term, score


_lex_cache: tuple[Any, Lexicon | None] = (None, None)
_lex_lock = threading.Lock()


def lexicon(settings: dict[str, Any]) -> Lexicon:
    global _lex_cache
    c = cfg(settings)
    packs = active_packs(settings)
    key = (tuple(id(p) for p in packs), tuple(c.get("custom_terms") or []), tuple(c.get("hidden_terms") or []))
    with _lex_lock:
        if _lex_cache[0] != key:
            _lex_cache = (key, Lexicon(packs, c.get("custom_terms") or [], c.get("hidden_terms") or []))
        return _lex_cache[1]


_english: set[str] | None = None


def english_words() -> set[str]:
    """Ordinary English words, which are never "corrected" into a medical term."""
    global _english
    if _english is None:
        try:
            with gzip.open(DATA_DIR / "english-words.txt.gz", "rt", encoding="utf-8") as f:
                _english = set(f.read().split())
        except OSError:
            log.warning("The English word list is missing; spelling fixes are more cautious")
            _english = set()
    return _english


def _is_english(word: str) -> bool:
    w = word.lower().replace("'", "").replace("’", "")
    words = english_words()
    if w in words:
        return True
    for suffix, add in (("s", ""), ("es", ""), ("ed", ""), ("ed", "e"), ("ing", ""), ("ing", "e"), ("ly", ""),
                        ("ies", "y"), ("ied", "y"), ("er", ""), ("est", ""), ("d", "")):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3 and w[: -len(suffix)] + add in words:
            return True
    return False


# ---- 1. the speech model's hint ----------------------------------------------

def speech_prompt(settings: dict[str, Any], count_tokens: Callable[[str], int] | None = None) -> str | None:
    """Words Whisper should expect: your dictionary, and in medical dictation your medical words
    and the main terms of your packs. The most important go last, because Whisper keeps the end
    of a prompt that's too long; here it's cut to fit instead."""
    from ..ai_cleanup import dictionary_words

    count = count_tokens or (lambda s: len(s) // 3 + 1)
    starred = [w for w in settings.get("dictionary_starred", []) if w.strip()]
    others = [w for w in dictionary_words(settings) if w not in starred]
    medical = enabled(settings) and cfg(settings).get("speech_hints", True)
    groups: list[list[str]] = []  # least important first
    if medical:
        hidden = {x.lower() for x in cfg(settings).get("hidden_terms") or []}
        # Your specialties and lists first: the core terms are the ones most likely to be heard right anyway.
        packs = sorted(active_packs(settings), key=lambda p: p.kind == "core")
        hints = [h for p in packs for h in p.hints if h.lower() not in hidden]
        groups.append(hints[:MAX_PACK_HINTS])
    groups.append(others)
    if medical:
        groups.append([t for t in cfg(settings).get("custom_terms") or [] if t.strip()])
    groups.append(starred)
    words: list[str] = []
    seen: set[str] = set()
    for group in reversed(groups):  # most important first, so they're the ones that fit
        for w in group:
            if w.lower() not in seen:
                seen.add(w.lower())
                words.append(w)
    if not words:
        return None
    lead = "Medical dictation. Vocabulary: " if medical else "Vocabulary: "
    kept: list[str] = []
    for w in words:
        if count(lead + ", ".join([w] + kept) + ".") > PROMPT_TOKENS:
            break
        kept.insert(0, w)
    return (lead + ", ".join(kept) + ".") if kept else None


# ---- 2. fixing the transcript --------------------------------------------------

_TOKEN = re.compile(r"[A-Za-z][A-Za-z'’-]*[A-Za-z]|[A-Za-z]")


def _shape(word: str, term: str) -> str:
    """Write the term with the capital the speaker's word had (start of a sentence)."""
    if term[:1].islower() and word[:1].isupper():
        return term[:1].upper() + term[1:]
    return term


def fix_aliases(text: str, lex: Lexicon) -> str:
    def repl(m: re.Match[str], term: str) -> str:
        # "... PO b.i.d." at the end of a sentence: its last dot was also the full stop.
        rest = m.string[m.end() :]
        ends = m.group().endswith(".") and (not rest.strip() or re.match(r"\s*\n|\s+[A-Z][a-z]", rest))
        return term + "." if ends else term

    for heard, term in sorted(lex.aliases.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(r"(?<![\w.])" + re.escape(heard) + r"(?![\w])", lambda m, t=term: repl(m, t), text, flags=re.IGNORECASE)
    return text


def fix_spelling(text: str, lex: Lexicon, protected: set[str] = frozenset()) -> tuple[str, list[tuple[str, str]]]:
    """Correct words that are close to exactly one medical term and aren't English words.
    Also joins a term the speech model split ("levo floxacin"). Returns (text, [(was, now)])."""
    tokens = list(_TOKEN.finditer(text))
    out, changes, pos, i = [], [], 0, 0
    known = {k.lower() for k in protected}
    while i < len(tokens):
        done = False
        # A term heard as two or three words.
        for n in (3, 2):
            group = tokens[i : i + n]
            if len(group) < n or any(not re.fullmatch(r"[ \t-]", text[a.end() : b.start()]) for a, b in zip(group, group[1:])):
                continue
            words = [t.group() for t in group]
            joined = "".join(words).lower().replace("’", "'")
            if len(joined) < 7 or any(len(w) < 2 or w.lower() in known for w in words):
                continue
            exact = lex.terms.get(joined)
            english = sum(_is_english(w) for w in words)
            # A whole term then a short word is two words: "Start enalapril at 5 mg" isn't IV enalaprilat.
            trailing_word = lex.knows(words[0]) and all(len(w) <= 3 and _is_english(w) for w in words[1:])
            if exact and " " not in exact and (english < n or len(joined) >= 9) and not trailing_word:
                match = exact
            # Joined by likeness only when every piece is a fragment: a whole term, or a short word
            # next to one, is a word of its own ("Metoprolol no longer needed" keeps its "no").
            elif (english < n and len(joined) >= 8
                  and not any(lex.knows(w) or _inflection_of_known(w, lex) or (len(w) <= 3 and _is_english(w)) for w in words)
                  and (best := lex.best(joined, 0.9)) and " " not in best[0]):
                match = best[0]
            else:
                continue
            out.append(text[pos : group[0].start()])
            out.append(_shape(words[0], match))
            changes.append((text[group[0].start() : group[-1].end()], match))
            pos, i, done = group[-1].end(), i + n, True
            break
        if done:
            continue
        tok = tokens[i]
        word = tok.group()
        low = word.lower()
        i += 1
        if len(word) < 6 or low in known or _is_known(word, lex) or _is_known(_us_spelling(low), lex):
            continue
        # A capital mid-sentence is probably a name: only very close matches.
        before = text[: tok.start()].rstrip()
        name_like = word[0].isupper() and before and before[-1] not in ".!?:\n"
        best = lex.best(word, 0.92 if name_like else 0.85)
        if not best or " " in best[0]:
            continue
        out.append(text[pos : tok.start()])
        out.append(_shape(word, best[0]))
        changes.append((word, best[0]))
        pos = tok.end()
    out.append(text[pos:])
    return "".join(out), changes


def _is_known(word: str, lex: Lexicon) -> bool:
    return lex.knows(word) or _is_english(word) or _inflection_of_known(word, lex)


def _us_spelling(word: str) -> str:
    """"haemoglobin" -> "hemoglobin": British spellings are right too, so they're left as they are."""
    return re.sub(r"our$", "or", word.replace("ae", "e").replace("oe", "e"))


def _inflection_of_known(word: str, lex: Lexicon) -> bool:
    """"replacements", "fractures": a medical word with an ending."""
    low = word.lower()
    return any(low.endswith(e) and lex.knows(low[: -len(e)] + add) for e, add in (("s", ""), ("es", ""), ("ies", "y"), ("al", ""), ("ic", "")))


# Spoken unit -> written unit. Only rewritten right after a number, so "a few milligrams" stays.
_UNIT_WORDS = {
    "milligram": "mg", "milligrams": "mg", "microgram": "mcg", "micrograms": "mcg", "gram": "g", "grams": "g",
    "kilogram": "kg", "kilograms": "kg", "kilo": "kg", "kilos": "kg", "milliliter": "mL", "milliliters": "mL",
    "millilitre": "mL", "millilitres": "mL", "liter": "L", "liters": "L", "litre": "L", "litres": "L",
    "milliequivalent": "mEq", "milliequivalents": "mEq", "millimole": "mmol", "millimoles": "mmol",
    "millimeter": "mm", "millimeters": "mm", "millimetre": "mm", "millimetres": "mm", "centimeter": "cm",
    "centimeters": "cm", "centimetre": "cm", "centimetres": "cm", "nanogram": "ng", "nanograms": "ng",
    "deciliter": "dL", "deciliters": "dL", "decilitre": "dL", "decilitres": "dL", "square meter": "m²",
    "square meters": "m²", "square metre": "m²", "square metres": "m²",
    "hour": "h", "hours": "h", "minute": "min", "minutes": "min", "kg": "kg", "mg": "mg", "mcg": "mcg", "ml": "mL",
}
_UNIT_PHRASES = [
    (r"millimeters? of mercury|millimetres? of mercury|mm of mercury", "mmHg"),
    (r"beats per minute", "bpm"),
    (r"breaths per minute", "breaths/min"),
    (r"per ?cent", "%"),
    (r"degrees? celsius", "°C"),
    (r"degrees? fahrenheit", "°F"),
]
_NUM = r"(?<![\w.])(?:\d+(?:\.\d+)?|\.\d+)"


def _zero(number: str) -> str:
    """".5" -> "0.5": a dose without its leading zero is read as 5 (on The Joint Commission's "do not use" list)."""
    return "0" + number if number.startswith(".") else number


def format_units(text: str) -> str:
    """"25 milligrams" -> "25 mg", "5 micrograms per kilogram per minute" -> "5 mcg/kg/min",
    "120 over 80" -> "120/80". Numbers keep their value; ".5 mg" gets its leading zero."""
    for phrase, unit in _UNIT_PHRASES:
        sep = "" if unit in ("%", "°C", "°F") else " "
        text = re.sub(rf"({_NUM})\s*(?:{phrase})\b", lambda m, u=unit, s=sep: _zero(m.group(1)) + s + u, text, flags=re.IGNORECASE)
    units = "|".join(sorted(map(re.escape, _UNIT_WORDS), key=len, reverse=True))
    # Hours and minutes are durations ("for 2 hours"): only shortened as part of a rate.
    rate = re.compile(rf"({_NUM})\s+({units})((?:\s+per\s+(?:{units}))*)\b", re.IGNORECASE)

    def unit_of(word: str) -> str:
        return _UNIT_WORDS[word.lower()]

    def repl(m: re.Match[str]) -> str:
        first = m.group(2)
        pers = re.findall(rf"per\s+({units})", m.group(3) or "", flags=re.IGNORECASE)
        if unit_of(first) in ("h", "min") and not pers:
            return m.group(0)
        return _zero(m.group(1)) + " " + "/".join([unit_of(first)] + [unit_of(p) for p in pers])

    text = rate.sub(repl, text)
    # Blood pressure: "BP 120 over 80" (only after "BP" or "pressure", or before "mmHg").
    bp = re.compile(r"(?i)(\b(?:BP|B\.P\.|pressures?)\b[^.\n\d]{0,20}|)\b(\d{2,3}) over (\d{2,3})\b(?=(\s*mmHg)?)")
    return bp.sub(lambda m: m.group(0) if not (m.group(1) or m.group(4)) else m.group(1) + m.group(2) + "/" + m.group(3), text)


def fix(text: str, settings: dict[str, Any]) -> str:
    """The rule-based medical fixes, before the AI step. Doesn't change any number."""
    if not enabled(settings) or not text:
        return text
    c = cfg(settings)
    lex = lexicon(settings)
    text = fix_aliases(text, lex)
    if c.get("fix_spelling", True):
        protected = set(settings.get("dictionary", [])) | set(settings.get("dictionary_starred", []))
        text, changes = fix_spelling(text, lex, protected)
        if changes:
            log.info("Medical spelling: %d word(s) corrected", len(changes))  # not which: patient data
    if c.get("format_units", True):
        text = format_units(text)
    return text


# ---- 3. hints for the AI cleanup ---------------------------------------------------

def ai_terms(text: str, settings: dict[str, Any], limit: int = 30) -> list[str]:
    """Medical terms that look like words of this dictation: given to the AI so it can tell what was
    meant, without a prompt of thousands of words."""
    lex = lexicon(settings)
    found: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        if term.lower() not in seen:
            seen.add(term.lower())
            found.append(term)

    for t in cfg(settings).get("custom_terms") or []:
        if t.strip() and re.search(r"(?<!\w)" + re.escape(t.strip()[:4]), text, re.IGNORECASE):
            add(t.strip())
    tokens = [t.group() for t in _TOKEN.finditer(text)]
    for i, word in enumerate(tokens):
        pair = tokens[i : i + 2]
        joined = "".join(pair) if len(pair) == 2 and not all(_is_english(w) for w in pair) else ""
        for chunk, threshold in ((word, 0.8), (joined, 0.86)):
            if len(chunk) < 5:
                continue
            low = chunk.lower()
            if low in lex.terms:
                if not _is_english(chunk):
                    add(lex.terms[low])
                continue
            if lex.knows(chunk) or _is_english(chunk):
                continue
            best = lex.best(chunk, threshold)
            if best:
                add(best[0])
        if len(found) >= limit:
            break
    return found[:limit]


def lookup(query: str, settings: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    """For the Medical page's "check a word": terms that start with or look like `query`."""
    q = query.strip().lower()
    if len(q) < 2:
        return []
    lex = lexicon(settings)
    hidden = {h.lower() for h in cfg(settings).get("hidden_terms") or []}
    rows = []
    for low, term in lex.terms.items():
        if low.startswith(q) or (len(q) >= 4 and q in low):
            rows.append((0 if low == q else 1 if low.startswith(q) else 2, low, term))
    rows.sort()
    out = [{"term": t, "pack": lex.source.get(low, ""), "hidden": False} for _, low, t in rows[:limit]]
    if len(out) < 5 and len(q) >= 5 and (best := lex.best(q, 0.75)) and best[0].lower() not in {o["term"].lower() for o in out}:
        out.append({"term": best[0], "pack": lex.source.get(best[0].lower(), ""), "hidden": False, "similar": True})
    for h in sorted(hidden):
        if h.startswith(q):
            term = next((t for p in active_packs(settings) for t in p.terms if t.lower() == h), h)
            out.append({"term": term, "pack": "Hidden", "hidden": True})
    return out[:limit]


def summary(settings: dict[str, Any]) -> dict[str, Any]:
    """What the Medical page lists: every pack with its size, and the folder for your own lists."""
    c = cfg(settings)
    chosen = set(c.get("specialties") or [])
    packs = []
    for pack in list(builtin_packs().values()) + user_packs():
        on = pack.kind in ("core", "user") or (pack.kind == "specialty" and pack.id in chosen) or (pack.kind == "fda" and c.get("fda_drug_names", True))
        packs.append({"id": pack.id, "title": pack.title, "description": pack.description, "kind": pack.kind,
                      "source": pack.source, "count": len(pack.terms), "on": on})
    return {"packs": packs, "user_dir": str(user_dir()), "total": len(lexicon(settings).terms)}
