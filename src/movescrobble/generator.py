"""A candidate track for the swim pool: who, how good a pick (score) and which bucket(s) it came from."""
from __future__ import annotations

from dataclasses import dataclass

Key = tuple[str, str]


def key(artist: str, title: str) -> Key:
    return (artist.casefold().strip(), title.casefold().strip())


@dataclass
class Candidate:
    artist: str
    title: str
    score: float
    sources: set[str]
