from datetime import datetime

import pytest

from movescrobble import canonical, flow, history
from movescrobble.config import Config
from movescrobble.scrobble import Scrobble


@pytest.fixture
def db(tmp_path):
    return history.connect(tmp_path / "h.db")


def played(db, artist, title, times=1):
    start = db.execute("SELECT COUNT(*) FROM scrobbles").fetchone()[0]
    db.executemany("INSERT INTO scrobbles(ts, artist, title) VALUES(?,?,?)",
                   [(start + i, artist, title) for i in range(times)])
    db.commit()


def resolver(db, listeners=None):
    known = listeners or {}
    return canonical.Resolver(db, lambda a, t: known.get((a, t), 0))


def test_most_listened_of_your_buckets_wins_even_with_fewer_plays(db):
    """The real case: the song sits under three spellings; the one everybody uses is the right one."""
    played(db, "Apparat", "An Echo Skips A Name", 2)
    played(db, "Apparat i Philipp Johann Thimm", "An Echo Skips A Name", 3)
    played(db, "Apparat, Philipp Johann Thimm", "An Echo Skips A Name", 1)
    r = resolver(db, {("Apparat", "An Echo Skips A Name"): 7042,
                      ("Apparat i Philipp Johann Thimm", "An Echo Skips A Name"): 4,
                      ("Apparat, Philipp Johann Thimm", "An Echo Skips A Name"): 419})
    assert r("Apparat, Philipp Johann Thimm", "An Echo Skips A Name") == ("Apparat", "An Echo Skips A Name")


def test_most_plays_decides_when_last_fm_cannot_be_asked(db):
    played(db, "Apparat", "Tilth", 2)
    played(db, "Apparat i Karyyn", "Tilth", 3)
    assert resolver(db)("Apparat, Karyyn", "Tilth") == ("Apparat i Karyyn", "Tilth")


def test_a_big_duo_bucket_is_not_abandoned_for_a_stray_solo_one(db):
    played(db, "Trent Reznor & Atticus Ross", "Hand Covers Bruise", 500)
    played(db, "Trent Reznor", "Hand Covers Bruise", 1)
    r = resolver(db, {("Trent Reznor & Atticus Ross", "Hand Covers Bruise"): 90000,
                      ("Trent Reznor", "Hand Covers Bruise"): 50})
    assert r("Trent Reznor & Atticus Ross", "Hand Covers Bruise")[0] == "Trent Reznor & Atticus Ross"


def test_one_of_the_credited_artists_is_enough(db):
    played(db, "YOUTH 83", "Slow Stride", 14)
    assert resolver(db)("YOUTH 83, Dan Farley, Dan Farley", "Slow Stride") == ("YOUTH 83", "Slow Stride")


def test_same_credit_spelled_differently_uses_your_spelling(db):
    played(db, "Trent Reznor & Atticus Ross & Boys Noize", "The Signal [MIXED]", 5)
    assert resolver(db)("Trent Reznor & Atticus Ross, Boys Noize", "The Signal [MIXED]") == (
        "Trent Reznor & Atticus Ross & Boys Noize", "The Signal [MIXED]")


def test_your_spelling_of_the_title_is_used(db):
    played(db, "Nine Inch Nails", "I Would for You", 9)
    assert resolver(db)("Nine Inch Nails", "I Would For You") == ("Nine Inch Nails", "I Would for You")


def test_comma_inside_a_band_name_survives(db):
    """yt-dlp writes a full-width comma when the comma belongs to the name."""
    played(db, "Mandy, Indiana", "Magazine", 6)
    assert resolver(db)("Mandy， Indiana", "Magazine") == ("Mandy, Indiana", "Magazine")
    assert resolver(db)("Mandy， Indiana", "Peach Fuzz") == ("Mandy, Indiana", "Peach Fuzz")


@pytest.mark.parametrize("version", ["An Echo Skips A Name (alternate take)", "An Echo Skips A Name (Remix)",
                                     "An Echo Skips A Name [Live]"])
def test_another_version_is_another_track(db, version):
    played(db, "Apparat", "An Echo Skips A Name", 40)
    assert resolver(db)("Apparat", version) == ("Apparat", version)


def test_a_cover_stays_with_its_artist(db):
    played(db, "Depeche Mode", "World In My Eyes", 53)
    assert resolver(db)("The Cure", "World In My Eyes") == ("The Cure", "World In My Eyes")


def test_a_lone_artist_is_never_widened_to_a_group_credit(db):
    played(db, "Apparat, Philipp Johann Thimm", "An Echo Skips A Name", 3)
    assert resolver(db)("Apparat", "An Echo Skips A Name") == ("Apparat", "An Echo Skips A Name")


def test_never_played_goes_where_last_fm_listeners_are(db):
    r = resolver(db, {("Clean Bandit, Jess Glynne", "Rather Be (feat. Jess Glynne)"): 668,
                      ("Clean Bandit", "Rather Be"): 46421})
    assert r("Clean Bandit, Jess Glynne", "Rather Be (feat. Jess Glynne)") == ("Clean Bandit", "Rather Be")


def test_your_bucket_beats_a_more_popular_page_you_never_used(db):
    played(db, "Plume, Fred Leonard, Laurent Parisi", "Song", 101)
    r = resolver(db, {("Plume", "Song"): 9000, ("Plume, Fred Leonard, Laurent Parisi", "Song"): 3})
    assert r("Plume, Fred Leonard, Laurent Parisi", "Song")[0] == "Plume, Fred Leonard, Laurent Parisi"


def test_never_played_band_name_with_comma_is_not_torn_apart(db):
    r = resolver(db, {("Black Country, New Road", "Sunglasses"): 80000, ("Black Country", "Sunglasses"): 2})
    assert r("Black Country, New Road", "Sunglasses") == ("Black Country, New Road", "Sunglasses")


def test_nothing_known_leaves_the_tags_alone(db):
    assert resolver(db)("A, B", "Song") == ("A, B", "Song")
    assert resolver(db)("", "Song") == ("", "Song")


def test_a_single_artist_track_costs_no_network(db):
    def boom(_a, _t):
        raise AssertionError("no lookup needed")

    assert canonical.Resolver(db, boom)("Solo", "Song") == ("Solo", "Song")


def test_listeners_treats_any_failure_as_unknown(db):
    def down(_params):
        raise OSError("offline")

    assert canonical.listeners_from_lastfm(db, down)("A", "T") == 0


def test_last_fm_gets_the_bucket_but_local_plays_keep_the_file_tags(db, monkeypatch):
    monkeypatch.setenv("LASTFM_API_KEY", "k")
    played(db, "Apparat", "Tilth", 3)
    db.execute("INSERT INTO sessions(id, planned_at, status) VALUES(1, 0, 'planned')")
    row = db.execute("SELECT * FROM sessions WHERE id=1").fetchone()
    local = Scrobble(datetime.now(), "Apparat, KÁRYYN", "Tilth", "Album", 200)
    settled = flow.Settled([], [local], 5)
    sent, brainz = [], []

    n = flow.submit_session(db, row, settled, Config(listenbrainz=True),
                            lastfm_submit=lambda s: (sent.extend(s) or len(s)), lb_submit=brainz.extend,
                            listeners=lambda _a, _t: 0)

    assert n == 1
    assert (sent[0].artist, sent[0].title, sent[0].album) == ("Apparat", "Tilth", "Album")
    assert brainz == sent
    assert [tuple(p) for p in db.execute("SELECT artist, title FROM plays")] == [("Apparat, KÁRYYN", "Tilth")]


def test_a_session_settled_meanwhile_is_not_sent_again(db):
    db.execute("INSERT INTO sessions(id, planned_at, status) VALUES(1, 0, 'planned')")
    row = db.execute("SELECT * FROM sessions WHERE id=1").fetchone()  # read while still planned
    db.execute("UPDATE sessions SET status='scrobbled' WHERE id=1")  # then settled by another run
    sent = []
    settled = flow.Settled([], [Scrobble(datetime.now(), "A", "T", "", 200)], 5)
    with pytest.raises(ValueError, match="settled or discarded meanwhile"):
        flow.submit_session(db, row, settled, Config(), lastfm_submit=sent.extend, listeners=lambda _a, _t: 0)
    assert sent == []
