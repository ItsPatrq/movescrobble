"""Match playlist entries to local files by fuzzy artist/title (+ duration)."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from .library import Track
from .ytm import Entry

MATCH, AMBIGUOUS, MISSING = "matched", "ambiguous", "missing"


_FOLD = str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ı": "i", "ß": "ss", "æ": "ae", "œ": "oe"})


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold().translate(_FOLD)
    s = re.sub(r"[\[(][^\])]*[\])]", " ", s)  # [MIXED], (feat. x), (Remastered)
    s = re.sub(r"\b(feat|ft)\.?\b.*", " ", s)
    s = re.sub(r"[^\w]+", " ", s)
    return " ".join(s.split())


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def score(entry: Entry, track: Track) -> float:
    s = 0.7 * _ratio(entry.title, track.title) + 0.3 * _ratio(entry.artist, track.artist)
    if entry.duration and abs(entry.duration - track.duration) <= 5:
        s = min(1.0, s + 0.05)
    return s


@dataclass(frozen=True)
class Result:
    entry: Entry
    track: Track | None
    score: float
    status: str


def match(entries: list[Entry], library: list[Track], hi: float = 0.85, lo: float = 0.6) -> list[Result]:
    out = []
    for e in entries:
        best, best_s = None, 0.0
        for t in library:
            sc = score(e, t)
            if sc > best_s:
                best, best_s = t, sc
        status = MATCH if best_s >= hi else AMBIGUOUS if best_s >= lo else MISSING
        out.append(Result(e, best if status != MISSING else None, best_s, status))
    return out
