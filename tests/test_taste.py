import os

import pytest

from swimscrobble import history, taste
from swimscrobble.affinity import Aff, ArtistAff
from swimscrobble.taste import Graph, Lastfm, diversified, recommend, spoken

DAY = 86400
NOW = 4000 * DAY


@pytest.fixture
def db(tmp_path):
    os.environ["LASTFM_API_KEY"] = "k"
    return history.connect(tmp_path / "h.db")


def hot(n=20.0, total=50):
    return Aff(n, n, total, int(NOW - DAY), 5)


def fake_lastfm(similar=None, tags=None, top=None, similar_tracks=None, calls=None):
    """A Last.fm transport answering from dicts keyed by artist (or (artist, track))."""
    similar, tags, top, similar_tracks = similar or {}, tags or {}, top or {}, similar_tracks or {}

    def tr(p):
        if calls is not None:
            calls.append(p)
        m, a = p["method"], p.get("artist")
        if m == "artist.getSimilar":
            return {"similarartists": {"artist": [{"name": b, "match": str(w)} for b, w in similar.get(a, [])]}}
        if m == "artist.getTopTags":
            return {"toptags": {"tag": [{"name": t, "count": c} for t, c in tags.get(a, [])]}}
        if m == "artist.getTopTracks":
            return {"toptracks": {"track": [{"name": t, "artist": {"name": a}} for t in top.get(a, [])]}}
        if m == "track.getSimilar":
            return {"similartracks": {"track": [{"name": t, "artist": {"name": b}, "match": str(w)}
                                                for b, t, w in similar_tracks.get((a, p["track"]), [])]}}
        if m == "user.getLovedTracks":
            return {"lovedtracks": {"track": []}}
        raise AssertionError(m)
    return tr


def test_lastfm_responses_are_cached_and_misses_too(db):
    calls = []
    tr = fake_lastfm(similar={"A": [("B", 0.5)]}, calls=calls)
    lf = Lastfm(db, tr, now=NOW, sleep=lambda _: None)
    assert lf.similar_artists("A") == [("B", 0.5)] and lf.similar_artists("a") == [("B", 0.5)]
    assert len(calls) == 1  # same artist, any case: one request

    def unknown(p):
        calls.append(p)
        return {"error": 6, "message": "The artist you supplied could not be found"}
    lf = Lastfm(db, unknown, now=NOW, sleep=lambda _: None)
    assert lf.tags("Nobody") == [] and lf.tags("Nobody") == [] and len(calls) == 2
    assert Lastfm(db, tr, now=NOW + 31 * DAY, sleep=lambda _: None).similar_artists("A") and len(calls) == 3  # stale


def test_tags_drop_generic_and_weak_ones(db):
    tr = fake_lastfm(tags={"A": [("Darkwave", 100), ("seen live", 90), ("A", 50), ("synthpop", 40), ("odd", 2)]})
    assert Lastfm(db, tr, now=NOW, sleep=lambda _: None).tags("A") == [("darkwave", 1.0), ("synthpop", 0.4)]


def test_walk_ranks_by_closeness_through_every_path():
    g = Graph()
    for seed in ("s1", "s2"):
        g.link(seed, "both", 1.0)  # reached from both of your artists
    g.link("s1", "one", 1.0)  # reached from one
    g.link("one", "far", 1.0)  # two steps away
    r = g.walk({"s1": 1.0, "s2": 1.0})
    ids = g.ids
    assert r[ids["both"]] > r[ids["one"]] > r[ids["far"]] > 0
    assert abs(r.sum() - 1) < 1e-9
    assert not Graph().walk({"x": 1.0}).size


def test_podcasts_in_the_history_are_not_taste():
    episodes = ["Why Are Games So Much Like Jobs?", "The State of Video Game Consoles Today", "Studium porażki | #152",
                "Harry Potter and the Cursed Video Game", "Kryzys gier AAA"]
    assert spoken(episodes, [])
    assert spoken(["A", "B", "C", "D", "E"], [("podcast", 1.0)])
    songs = ["Enjoy the Silence", "Personal Jesus", "Never Let Me Go", "Strangelove", "Handcuffed to a Parking Meter (feat. Sparks)"]
    assert not spoken(songs, [])
    assert not spoken(songs[:3] + episodes[:2], [("synthpop", 1.0)])  # a band with long titles but real genres


def test_one_cluster_does_not_take_every_slot():
    scores = {"a1": 0.9, "a2": 0.85, "a3": 0.8, "b1": 0.7}
    owner = {"a1": "A", "a2": "A", "a3": "A", "b1": "B"}
    picks = diversified(list(scores), scores, owner, 3)
    assert list(picks) == ["a1", "b1", "a2"] and picks["a2"] == pytest.approx(0.85 * taste.SPREAD)


def test_recommend_end_to_end(db):
    tracks = {("Depeche Mode", "Enjoy the Silence"): hot(30), ("Nine Inch Nails", "Closer"): hot(20),
              ("Talk Show", "Why Are Games So Much Like Jobs?"): hot(40), ("Talk Show", "Is GTA VI Late Again Today?"): hot(40),
              ("Talk Show", "The State of Video Game Consoles"): hot(40), ("Talk Show", "Harry Potter and the Cursed Game"): hot(40),
              ("Talk Show", "Does Video-Game Marketing Really Work?"): hot(40)}
    artists = {"Depeche Mode": ArtistAff(30, int(NOW), 900), "Nine Inch Nails": ArtistAff(20, int(NOW), 700),
               "Talk Show": ArtistAff(40, int(NOW), 200)}
    db.execute("INSERT INTO blocked(artist, title) VALUES('Pop Star', '')")
    tr = fake_lastfm(
        similar={"Depeche Mode": [("Both Band", 0.8), ("Synth Only", 0.6), ("Pop Star", 0.9)],
                 "Nine Inch Nails": [("Both Band", 0.8), ("Industrial Only", 0.6)],
                 "Talk Show": [("Other Podcast", 1.0)]},
        tags={"Depeche Mode": [("synthpop", 100), ("darkwave", 60)], "Nine Inch Nails": [("industrial", 100)],
              "Both Band": [("darkwave", 100), ("industrial", 80)], "Synth Only": [("synthpop", 100)],
              "Industrial Only": [("industrial", 100)], "Pop Star": [("pop", 100)], "Talk Show": [("podcast", 100)]},
        top={"Both Band": ["Dark Hit", "Deep Cut"], "Synth Only": ["Synth Hit"], "Industrial Only": ["Noise Hit"],
             "Depeche Mode": ["Enjoy the Silence", "Unheard DM"], "Nine Inch Nails": ["Closer", "Unheard NIN"],
             "Pop Star": ["Radio Hit"]},
        similar_tracks={("Depeche Mode", "Enjoy the Silence"): [("Synth Only", "Similar To Silence", 0.9)]},
    )
    t = recommend(db, "", tracks, artists, lf=Lastfm(db, tr, now=NOW, sleep=lambda _: None))
    s = t.artist_score
    assert s["both band"] > s["synth only"] and s["both band"] > s["industrial only"]  # two of your artists lead there
    assert "pop star" not in s and "other podcast" not in s and "talk show" not in s  # blocked / talk: never
    recs = [(a, title) for a, title, _ in t.recs]
    assert ("Both Band", "Dark Hit") in recs and ("Synth Only", "Similar To Silence") in recs
    assert ("Depeche Mode", "Unheard DM") in recs  # fresh: a known artist's track you have not played
    assert ("Depeche Mode", "Enjoy the Silence") not in recs  # already played: history handles it
    assert not any(a == "Pop Star" for a, _ in recs)
    assert set(t.why["both band"]) == {"Depeche Mode", "Nine Inch Nails"}
    assert t.fit("Both Band", "Dark Hit") >= t.fit("Both Band", "Never Heard Of") == s["both band"]
