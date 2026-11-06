from types import SimpleNamespace

import pytest

from swimscrobble import history, ytmirror
from swimscrobble.config import Config


class FakeYT:
    def __init__(self, tracks=None, missing=False, refuse=False, error=None):
        self.tracks, self.missing, self.refuse, self.error = tracks or [], missing, refuse, error
        self.calls = []

    def get_playlist(self, playlist, limit):
        if self.error:
            raise self.error
        if self.missing:
            raise KeyError("Unable to find 'contents'")
        return {"tracks": self.tracks}

    def create_playlist(self, title, description, privacy, ids):
        self.calls.append(("create", title, privacy, ids))
        return {"error": "no"} if self.refuse else "PLnew"

    def remove_playlist_items(self, playlist, tracks):
        self.calls.append(("remove", playlist, [t["videoId"] for t in tracks]))

    def add_playlist_items(self, playlist, ids, duplicates):
        self.calls.append(("add", playlist, ids, duplicates))


@pytest.fixture
def db(tmp_path):
    return history.connect(tmp_path / "h.db")


def manifest(*names):
    """Listed out of order on purpose: play order is `position`."""
    tracks = [{"position": i, "artist": "A", "title": n} for i, n in enumerate(names, 1)]
    return {"tracks": list(reversed(tracks))}


def video(_artist, title):
    return f"v{title}"


def remember(db, playlist="PLold"):
    history.set_meta(db, ytmirror.META_KEY, playlist)
    db.commit()


def on_yt(*ids):
    return [{"videoId": v, "setVideoId": f"s{v}"} for v in ids]


def test_first_time_creates_a_private_playlist_in_play_order(db):
    yt = FakeYT()
    out = ytmirror.update(db, Config(), manifest("1", "2"), yt, video)
    assert yt.calls == [("create", "swimscrobble: on the headphones", "PRIVATE", ["v1", "v2"])]
    assert history.get_meta(db, ytmirror.META_KEY) == "PLnew"
    assert out == "YouTube playlist updated (2/2 tracks)"


def test_a_new_playlist_replaces_the_contents_of_the_same_youtube_playlist(db):
    remember(db)
    yt = FakeYT(on_yt("vold"))
    ytmirror.update(db, Config(), manifest("1", "2"), yt, video)
    assert yt.calls == [("remove", "PLold", ["vold"]), ("add", "PLold", ["v1", "v2"], True)]


def test_a_changed_order_counts_as_a_change(db):
    remember(db)
    yt = FakeYT(on_yt("v2", "v1"))
    ytmirror.update(db, Config(), manifest("1", "2"), yt, video)
    assert yt.calls[-1] == ("add", "PLold", ["v1", "v2"], True)


def test_already_in_line_changes_nothing_and_says_nothing(db):
    remember(db)
    yt = FakeYT(on_yt("v1", "v2"))
    assert ytmirror.update(db, Config(), manifest("1", "2"), yt, video) == ""
    assert yt.calls == []


def test_an_empty_playlist_on_youtube_is_filled_without_removing(db):
    remember(db)
    yt = FakeYT([])
    ytmirror.update(db, Config(), manifest("1"), yt, video)
    assert yt.calls == [("add", "PLold", ["v1"], True)]


def test_a_playlist_you_deleted_on_youtube_is_created_again(db):
    remember(db)
    yt = FakeYT(missing=True)
    ytmirror.update(db, Config(), manifest("1"), yt, video)
    assert yt.calls[0][0] == "create" and history.get_meta(db, ytmirror.META_KEY) == "PLnew"


def test_a_network_error_does_not_create_a_second_playlist(db):
    remember(db)
    yt = FakeYT(error=OSError("offline"))
    assert ytmirror.update(db, Config(), manifest("1"), yt, video) == "YouTube playlist FAILED: offline"
    assert yt.calls == [] and history.get_meta(db, ytmirror.META_KEY) == "PLold"


def test_youtube_refusing_is_reported_and_no_id_is_kept(db):
    out = ytmirror.update(db, Config(), manifest("1"), FakeYT(refuse=True), video)
    assert "FAILED" in out and history.get_meta(db, ytmirror.META_KEY) is None


def test_a_song_not_on_youtube_is_left_out_and_counted(db):
    out = ytmirror.update(db, Config(), manifest("1", "2"), yt := FakeYT(), lambda _a, t: None if t == "2" else "v1")
    assert yt.calls[0][3] == ["v1"] and out == "YouTube playlist updated (1/2 tracks)"


def test_found_videos_are_remembered_and_misses_tried_again(db):
    asked = []

    def find(_a, title):
        asked.append(title)
        return None if title == "2" else "v1"

    ytmirror.update(db, Config(), manifest("1", "2"), FakeYT(), find)
    ytmirror.update(db, Config(), manifest("1", "2"), FakeYT(), find)
    assert asked == ["1", "2", "2"]


def test_a_track_twice_in_the_playlist_goes_once_to_youtube(db):
    ytmirror.update(db, Config(), manifest("1", "1"), yt := FakeYT(), video)
    assert yt.calls[0][3] == ["v1"]


def test_nothing_found_touches_nothing(db):
    remember(db)
    yt = FakeYT(on_yt("vold"))
    assert ytmirror.update(db, Config(), manifest("1"), yt, lambda _a, _t: None) == ""
    assert yt.calls == []


def test_without_login_there_is_nothing_to_do(db):
    assert ytmirror.update(db, Config(), manifest("1"), None, video) == ""


def test_a_shell_edit_reaches_youtube(tmp_path, monkeypatch, db):
    from swimscrobble import editing

    seen = []
    monkeypatch.setattr(ytmirror, "update", lambda _db, _cfg, m: seen.append([t["title"] for t in m["tracks"]]))
    vol = tmp_path / "Volumes"
    (vol / "SWIM PRO" / "Music").mkdir(parents=True)
    tracks = [{"position": i, "artist": "A", "title": str(i), "file": f"/{i}.mp3", "staged": f"00{i} - {i}.mp3"}
              for i in (1, 2)]
    history.add_session(db, {"tracks": tracks}, 60, None, "x")
    editing.remove_tracks(Config(volumes_dir=str(vol)), db, {1})
    assert seen == [["2"]]


def test_the_real_client_stays_off_without_auth(monkeypatch):
    monkeypatch.undo()
    assert ytmirror.open_client(SimpleNamespace(ytm_mirror=False, ytm_auth_path="x")) is None
    assert ytmirror.open_client(SimpleNamespace(ytm_mirror=True, ytm_auth_path=None)) is None
