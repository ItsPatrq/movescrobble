"""End-to-end CLI runs against real (ffmpeg-generated) MP3s."""
import json
import shutil
import subprocess

import pytest

from movescrobble.cli import main

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture
def music(tmp_path):
    d = tmp_path / "music"
    d.mkdir()
    for i in range(8):
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={300 + i * 50}:duration=100",
             "-metadata", f"artist=Artist{i}", "-metadata", f"title=Song{i}", "-metadata", "TBPM=120",
             str(d / f"{i}.mp3")],
            check=True,
        )
    return d


def test_plan_stage_and_scrobble(music, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MOVESCROBBLE_HOME", str(tmp_path / "home"))
    manifest, stage = tmp_path / "s.json", tmp_path / "stage"
    main(["plan", str(music), "--minutes", "5.2", "--playlist-minutes", "10.4", "--seed", "1",
          "--out", str(manifest), "--stage", str(stage)])
    m = json.loads(manifest.read_text())
    assert len(m["tracks"]) == 6  # 100s tracks into 2 x 312s buffered budget
    assert m["minutes"] == 5.2
    assert sorted(p.name for p in stage.iterdir())[0].startswith("001 - ")
    assert m["tracks"][0]["duration"] == 100

    out = tmp_path / "out.csv"
    main(["scrobble", str(manifest), "--start", "2026-10-25T07:00", "--skip", "2", "--out", str(out)])
    rows = out.read_text().strip().splitlines()
    assert len(rows) == 4  # header + 3: swim is 312s, so only the first 3 of the other 5 tracks count
    assert rows[1].startswith("2026-10-25T07:00:00") and rows[2].startswith("2026-10-25T07:01:40")

    main(["scrobble", str(manifest), "--start", "2026-10-25T07:00", "--played-minutes", "2.5", "--out", str(out)])
    assert len(out.read_text().strip().splitlines()) == 3  # 100s + 50s(half) counts, 2nd track = 50s of 100 -> yes


def test_scrobble_stale_start_refuses_submit(music, tmp_path, monkeypatch):
    monkeypatch.setenv("MOVESCROBBLE_HOME", str(tmp_path / "home"))
    manifest = tmp_path / "s.json"
    main(["plan", str(music), "--minutes", "5.2", "--playlist-minutes", "10.4", "--seed", "1", "--out", str(manifest)])
    monkeypatch.setenv("LASTFM_API_KEY", "k")
    monkeypatch.setenv("LASTFM_API_SECRET", "s")
    with pytest.raises(SystemExit, match="14-day"):
        main(["scrobble", str(manifest), "--start", "2020-01-01T07:00", "--submit"])
