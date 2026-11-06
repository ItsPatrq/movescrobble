import hashlib
from datetime import datetime, timedelta

import pytest

from movescrobble import lastfm
from movescrobble.scrobble import Scrobble


def sc(i, when):
    return Scrobble(when + timedelta(minutes=3 * i), "Art", f"T{i}", "Alb", 180)


def test_sign_ignores_format_and_sorts():
    p = {"b": "2", "a": "1", "format": "json"}
    assert lastfm.sign(p, "sec") == hashlib.md5(b"a1b2sec").hexdigest()


def test_window_flags_old_and_future():
    now = datetime(2026, 10, 25, 12).timestamp()
    old = sc(0, datetime(2026, 10, 1))
    ok = sc(0, datetime(2026, 10, 24))
    future = sc(0, datetime(2026, 10, 26))
    assert lastfm.check_window([old, ok, future], now) == [old, future]


def test_submit_batches_of_50(monkeypatch):
    monkeypatch.setenv("LASTFM_API_KEY", "k")
    monkeypatch.setenv("LASTFM_API_SECRET", "s")
    calls = []

    def fake(body):
        calls.append(body)
        n = sum(1 for k in body if k.startswith("artist["))
        return {"scrobbles": {"@attr": {"accepted": n}}}

    items = [sc(i, datetime(2026, 10, 24)) for i in range(120)]
    assert lastfm.submit(items, "sk", fake) == 120
    assert len(calls) == 3
    assert calls[0]["method"] == "track.scrobble" and calls[0]["sk"] == "sk"
    assert "api_sig" in calls[0]


def test_error_raises(monkeypatch):
    monkeypatch.setenv("LASTFM_API_KEY", "k")
    monkeypatch.setenv("LASTFM_API_SECRET", "s")
    with pytest.raises(RuntimeError, match="Invalid"):
        lastfm.submit([sc(0, datetime(2026, 10, 24))], "sk", lambda b: {"error": 9, "message": "Invalid session"})
