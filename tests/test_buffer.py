from movescrobble.session import played_part


def manifest(n=24, dur=300, minutes=60):
    return {"minutes": minutes, "tracks": [
        {"position": i + 1, "file": f"{i}.mp3", "staged": f"{i + 1:03d}", "artist": "A", "title": f"T{i}",
         "album": "", "duration": dur} for i in range(n)]}


def test_only_swim_length_counts_from_buffered_playlist():
    plan, secs = played_part(manifest())
    assert len(plan) == 12 and secs == 3600


def test_played_minutes_overrides():
    plan, secs = played_part(manifest(), played_minutes=30)
    assert len(plan) == 6 and secs == 1800
    plan, _ = played_part(manifest(), played_minutes=90)  # swam longer, still within the buffer
    assert len(plan) == 18


def test_skips_pull_later_tracks_into_the_swim():
    plan, _ = played_part(manifest(), skipped={1, 2})
    assert [p.position for p in plan][:2] == [3, 4] and len(plan) == 12


def test_short_playlist_never_exceeds_total():
    plan, secs = played_part(manifest(n=5), played_minutes=600)
    assert len(plan) == 5 and secs == 1500


def test_old_manifest_without_minutes_counts_everything():
    m = manifest(n=4)
    del m["minutes"]
    plan, secs = played_part(m)
    assert len(plan) == 4 and secs == 1200
