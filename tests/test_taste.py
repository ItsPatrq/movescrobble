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
    songs = ["Enjoy the Silence", "Personal Jesus", "Never Let Me Go", "Strangelove",
             "Handcuffed to a Parking Meter (feat. Sparks)"]
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
              ("Talk Show", "The State of Video Game Consoles"): hot(40),
              ("Talk Show", "Harry Potter and the Cursed Game"): hot(40),
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


def test_tuning_is_saved_and_steering_replaces_the_opposite_wish(db):
    t = taste.Tuning.load(db)
    assert (t.reach, t.genre, t.more) == (1, 1, [])
    t.turn("reach", 1)
    t.turn("reach", 1)  # stops at the last answer
    t.steer("Kyuss", True)
    t.steer("Zedd", False)
    t.steer("kyuss", False)  # changed your mind: now less
    t.save(db)
    back = taste.Tuning.load(db)
    assert back.answer("reach") == "far afield" and back.value("reach") == 0.15
    assert back.more == [] and back.less == ["Zedd", "kyuss"]


def test_more_and_less_like_steer_the_walk(db):
    tracks = {("Depeche Mode", "Enjoy the Silence"): hot(30)}
    artists = {"Depeche Mode": ArtistAff(30, int(NOW), 900)}
    tr = fake_lastfm(similar={"Depeche Mode": [("Synth A", 0.8), ("Synth B", 0.7)], "Kyuss": [("Desert Band", 0.9)]},
                     tags={"Depeche Mode": [("synthpop", 100)], "Synth A": [("synthpop", 100)],
                           "Synth B": [("synthpop", 100)], "Kyuss": [("stoner rock", 100)],
                           "Desert Band": [("stoner rock", 100)]})
    lf = Lastfm(db, tr, now=NOW, sleep=lambda _: None)
    plain = recommend(db, "", tracks, artists, lf=lf, tuning=taste.Tuning())
    assert "desert band" not in plain.artist_score and "synth a" in plain.artist_score
    steered = recommend(db, "", tracks, artists, lf=lf, tuning=taste.Tuning(more=["Kyuss"], less=["Synth A"]))
    assert steered.artist_score["desert band"] > 0 and "Kyuss" in steered.seeds
    assert "synth a" not in steered.artist_score  # less like: left out, and its neighbourhood pulled down


def test_graph_page_lists_your_artists_and_where_they_lead(tmp_path):
    from swimscrobble import graphview

    t = taste.Taste(artist_score={"new band": 0.6, "old band": 0.4}, seeds={"Depeche Mode": 2.0},
                    why={"new band": ["Depeche Mode"]}, names={"new band": "New Band"}, known={"old band"})
    data = graphview.graph_data(t, owned={"old band"})
    kinds = {n["id"]: (n["kind"], n["owned"]) for n in data["nodes"]}
    assert kinds == {"depeche mode": ("seed", False), "new band": ("new", False), "old band": ("fresh", True)}
    assert data["links"] == [{"source": "depeche mode", "target": "new band", "strength": 1.0}]
    page = graphview.write(t, path=tmp_path / "g.html").read_text()
    assert "New Band" in page and "d3/7.8.5" in page and "__DATA__" not in page


def test_terminal_graph_connects_you_your_artists_and_their_picks():
    from swimscrobble import tgraph

    t = taste.Taste(artist_score={"pick a": 0.9, "pick b": 0.5, "far": 0.4}, seeds={"Seed One": 2.0, "Seed Two": 1.0},
                    why={"pick a": ["Seed One", "Seed Two"], "pick b": ["Seed Two"], "far": ["Somebody Else"]},
                    names={"pick a": "Pick A", "pick b": "Pick B"}, known={"pick b"})
    nodes, edges = tgraph.build(t, owned={"pick a"})
    assert [n.label for n in nodes] == ["you", "Seed One", "Seed Two", "Pick A", "Pick B"]  # "far": no seed of yours
    assert (1, 3, 1.0) in edges and (2, 3, 0.5) in edges and (0, 1, 1.0) in edges  # the second path, dotted
    tgraph.layout(nodes, edges, 80, 24)
    assert all(0 <= n.x < 80 and 0 <= n.y < 24 for n in nodes)
    rows = tgraph.render(nodes, edges, 80, 24, selected=3)
    screen = "\n".join(r.plain for r in rows)
    assert "Pick A ♪" in screen and "Seed Two" in screen and len(rows) == 24
    # two seeds sit straight above and below you: arrows go to the nearest node that way, or nowhere
    assert tgraph.step(nodes, 0, "up") == 1 and tgraph.step(nodes, 0, "down") == 2 and tgraph.step(nodes, 0, "left") == 0
