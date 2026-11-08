"""Keyboard navigation: widgets and the shell driven by scripted key presses (no terminal needed)."""
import contextlib
import io
import json
import re

import pytest
from rich.console import Console
from test_shell import Fake

from movescrobble import flow, history, keys
from movescrobble.config import Config
from movescrobble.energy import Features
from movescrobble.library import Track
from movescrobble.shell import Shell
from movescrobble.ui import Item, KeyUI


def ui_with(*pressed):
    out = io.StringIO()
    seq = iter(pressed)

    def read():
        try:
            return next(seq)
        except StopIteration:
            raise EOFError from None

    return KeyUI(Console(file=out, width=120, force_terminal=False, color_system=None), read, contextlib.nullcontext), out


ITEMS = [Item("p", "Playlist"), Item("r", "Reroll"), Item("s", "Stats"), Item("q", "Quit")]


def test_menu_enter_selects_the_highlighted_item():
    ui, _ = ui_with("down", "down", "enter")
    assert ui.menu(ITEMS) == "s"


def test_menu_wraps_around_and_accepts_vim_keys():
    ui, _ = ui_with("up", "enter")
    assert ui.menu(ITEMS) == "q"  # up from the first wraps to the last
    ui, _ = ui_with("j", "k", "j", "enter")
    assert ui.menu(ITEMS) == "r"


def test_menu_shortcut_letter_selects_immediately_and_esc_cancels():
    ui, _ = ui_with("s")
    assert ui.menu(ITEMS) == "s"
    ui, _ = ui_with("esc")
    assert ui.menu(ITEMS) is None


def test_menu_unknown_keys_are_ignored_and_home_end_work():
    ui, _ = ui_with("x", "left", "end", "enter")
    assert ui.menu(ITEMS) == "q"
    ui, _ = ui_with("end", "home", "enter")
    assert ui.menu(ITEMS) == "p"


def test_select_rows_extra_items_and_cancel():
    rows = ["a", "b", "c"]
    ui, _ = ui_with("down", "enter")
    assert ui.select(rows) == 1
    ui, _ = ui_with("y")
    assert ui.select(rows, extra=[Item("y", "Search YouTube")]) == "y"
    ui, _ = ui_with("end", "enter")
    assert ui.select(rows, extra=[Item("y", "Search YouTube")]) == "y"  # the extra row is part of the list
    ui, _ = ui_with("esc")
    assert ui.select(rows) is None


def test_multiselect_space_ticks_a_toggles_all_enter_confirms():
    ui, _ = ui_with("space", "down", "down", "space", "enter")
    assert ui.multiselect(["a", "b", "c"]) == [0, 2]
    ui, _ = ui_with("a", "enter")
    assert ui.multiselect(["a", "b", "c"]) == [0, 1, 2]
    ui, _ = ui_with("a", "a", "down", "enter")
    assert ui.multiselect(["a", "b", "c"]) == [1]  # nothing ticked: the highlighted row
    ui, _ = ui_with("space", "esc")
    assert ui.multiselect(["a", "b"]) is None


def test_confirm_defaults_and_letters():
    for pressed, default, expected in (("enter", True, True), ("enter", False, False), ("y", False, True),
                                       ("n", True, False), ("esc", True, False)):
        ui, _ = ui_with(pressed)
        assert ui.confirm("sure?", default) is expected


def test_key_reader_decodes_escape_sequences(monkeypatch):
    import os

    r, w = os.pipe()
    try:
        for raw, name in ((b"\x1b[A", "up"), (b"\x1b[B", "down"), (b"\x1b[C", "right"), (b"\x1b[D", "left"), (b"\r", "enter"),
                          (b" ", "space"), (b"\x7f", "backspace"), (b"q", "q"), ("é".encode(), "é"), (b"\x1b", "esc"),
                          (b"\x1b[3~", "unknown")):
            os.write(w, raw)
            assert keys.read_key(r) == name
        os.write(w, b"\x03")
        with pytest.raises(KeyboardInterrupt):
            keys.read_key(r)
        os.write(w, b"\x04")
        with pytest.raises(EOFError):
            keys.read_key(r)
    finally:
        os.close(r)
        os.close(w)


# -- the shell, driven by keys ----------------------------------------------------------------------
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MOVESCROBBLE_HOME", str(tmp_path / "home"))
    music = tmp_path / "music"
    music.mkdir()
    lib = []
    for i in range(30):
        f = music / f"{i:02d} - song{i}.mp3"
        f.write_bytes(f"audio{i}".encode())
        lib.append(Track(f, f"Artist{i % 6}", f"Song{i}", "Alb", 300))
    vol = tmp_path / "Volumes"
    (vol / "SWIM PRO" / "Music").mkdir(parents=True)
    cfg = Config(music_dir=str(music), volumes_dir=str(vol), minutes=60, loudness_target=0)  # fake audio
    return cfg, history.connect(tmp_path / "h.db"), lib, vol / "SWIM PRO" / "Music", music


def keyed_shell(env, presses, lines=()):
    cfg, db, lib, folder, music = env
    fake = Fake(lib, music)
    out = io.StringIO()
    console = Console(file=out, width=140, force_terminal=False, color_system=None)
    seq, text = iter(presses), iter(lines)

    def read():
        try:
            return next(seq)
        except StopIteration:
            raise EOFError from None

    ui = KeyUI(console, read, contextlib.nullcontext)
    shell = Shell(cfg, db, fake.actions(), console, ask=lambda p: next(text, ""), keys=ui)
    return shell, fake, out


def staged(env):
    cfg, db, lib, *_ = env
    flow.on_connect(cfg, db, library_loader=lambda _: lib, notify=lambda m: None,
                    feature_of=lambda t: Features(120, -7, 150, 0.6, 0.8))
    return json.loads(history.pending_session(db)["manifest"])["tracks"]


def test_main_menu_navigates_with_arrows_and_quits(env):
    shell, fake, out = keyed_shell(env, ["down", "down", "down", "enter", "any", "q"])  # Stats, then back, then quit
    with pytest.raises(SystemExit):  # EOF after the scripted keys would also exit; here 'q' ends it cleanly
        shell.run()
        raise SystemExit
    assert "Top artists" in re.sub(r"\s+", " ", out.getvalue())


def test_shortcut_runs_an_action_from_the_menu_and_esc_quits(env):
    shell, _, out = keyed_shell(env, ["s", "any", "esc"])
    shell.run()
    assert "Top artists" in out.getvalue()


def test_reroll_from_the_menu_then_edit_the_playlist_with_the_cursor(env):
    cfg, db, lib, folder, _ = env
    # r = reroll: confirm sync? (n), workout (Enter = first), seed line; then the playlist editor: quit; pause; quit
    shell, fake, out = keyed_shell(env, ["r", "n", "enter", "q", "any", "q"], lines=[""])
    shell.run()
    tracks = json.loads(history.pending_session(db)["manifest"])["tracks"]
    assert len(tracks) >= 10 and len(list(folder.iterdir())) == len(tracks)


def test_playlist_editor_carries_a_track_down_with_arrows(env):
    cfg, db, lib, folder, _ = env
    before = staged(env)
    shell, _, _ = keyed_shell(env, ["enter", "down", "down", "enter", "q"])  # pick up #1, carry to #3, drop, leave
    shell.playlist_keys()
    after = json.loads(history.pending_session(db)["manifest"])["tracks"]
    assert [t["file"] for t in after[:3]] == [before[1]["file"], before[2]["file"], before[0]["file"]]
    assert (folder / f"003 - {__import__('pathlib').Path(before[0]['file']).name}").exists()


def test_playlist_editor_escape_while_carrying_changes_nothing(env):
    cfg, db, lib, folder, _ = env
    before = staged(env)
    shell, _, _ = keyed_shell(env, ["enter", "down", "down", "esc", "q"])
    shell.playlist_keys()
    after = json.loads(history.pending_session(db)["manifest"])["tracks"]
    assert [t["file"] for t in after] == [t["file"] for t in before]


def test_playlist_editor_delete_asks_for_confirmation(env):
    cfg, db, lib, folder, _ = env
    before = staged(env)
    shell, _, _ = keyed_shell(env, ["down", "d", "n", "q"])  # decline
    shell.playlist_keys()
    assert len(json.loads(history.pending_session(db)["manifest"])["tracks"]) == len(before)
    shell, _, _ = keyed_shell(env, ["down", "d", "y", "q"])  # accept: removes #2
    shell.playlist_keys()
    after = json.loads(history.pending_session(db)["manifest"])["tracks"]
    assert len(after) == len(before) - 1 and after[1]["file"] == before[2]["file"]


def test_playlist_editor_add_track_picks_from_a_list_with_keys(env):
    cfg, db, lib, folder, _ = env
    before = staged(env)
    used = {t["file"] for t in before}
    new = next(t for t in lib if str(t.path) not in used and len(t.title) == 6)
    # a -> search line -> select first match (enter) -> position line "1" -> back with q
    shell, _, _ = keyed_shell(env, ["a", "enter", "q"], lines=[new.title.lower(), "1"])
    shell.playlist_keys()
    after = json.loads(history.pending_session(db)["manifest"])["tracks"]
    assert after[0]["file"] == str(new.path) and len(after) == len(before) + 1


def test_wishlist_checklist_downloads_the_ticked_rows(env, tmp_path):
    f = tmp_path / "home" / "wishlist.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("Alpha - One\t[rec]\tu\nBeta - Two\t[fresh]\tu\nGamma - Three\t[obsession]\tu\n")
    shell, fake, out = keyed_shell(env, ["w", "space", "down", "down", "space", "enter", "any", "q"])
    shell.run()
    assert fake.calls.count(("download", "wish1")) == 2  # rows 1 and 3
    assert out.getvalue().count("downloaded") == 2


def test_scrobble_uses_a_yes_no_menu(env):
    cfg, db, lib, folder, _ = env
    staged(env)
    db.execute("UPDATE sessions SET planned_at = planned_at - 7200")
    db.commit()
    # c -> minutes line, skipped line -> preview -> confirm Yes (y) -> stage next? No (n) -> pause -> quit
    shell, fake, out = keyed_shell(env, ["c", "y", "n", "any", "q"], lines=["45", ""])
    shell.run()
    assert fake.calls[-1][0] == "submit" and history.pending_session(db) is None


def test_scrobble_declined_with_the_confirm_menu_keeps_the_session(env):
    cfg, db, lib, folder, _ = env
    staged(env)
    db.execute("UPDATE sessions SET planned_at = planned_at - 7200")
    db.commit()
    shell, fake, _ = keyed_shell(env, ["c", "n", "any", "q"], lines=["", ""])
    shell.run()
    assert history.pending_session(db) is not None and not fake.calls


def test_skip_confirms_with_keys(env):
    cfg, db, *_ = env
    staged(env)
    shell, _, _ = keyed_shell(env, ["k", "y", "n", "any", "q"])  # skip, confirm, keep the files
    shell.run()
    assert history.pending_session(db) is None


def test_a_shortcut_letter_beats_vim_movement():
    ui, _ = ui_with("k")
    assert ui.menu([Item("p", "Playlist"), Item("k", "Skip")]) == "k"  # 'k' is a shortcut here, not "up"
    ui, _ = ui_with("j", "enter")
    assert ui.menu([Item("p", "Playlist"), Item("r", "Reroll")]) == "r"  # no j/k shortcuts: vim keys move


def test_plain_flag_and_env_force_line_mode(env, monkeypatch):
    from movescrobble import cli
    built = {}
    monkeypatch.setattr("movescrobble.keys.available", lambda: True)
    monkeypatch.setattr("movescrobble.shell.Shell.run", lambda self: built.update(keys=self.keys))
    cli.cmd_shell(type("A", (), {"plain": False})())
    assert built["keys"] is not None
    monkeypatch.setenv("MOVESCROBBLE_PLAIN", "1")
    cli.cmd_shell(type("A", (), {"plain": False})())
    assert built["keys"] is None


def test_end_of_input_leaves_the_shell_quietly(env):
    shell, _, _ = keyed_shell(env, [])
    shell.run()  # no keys at all: EOF in the menu returns instead of raising


def fake_taste():
    from movescrobble.taste import Taste

    return Taste(artist_score={"artist1": 0.8, "new artist": 0.6}, seeds={"Artist0": 2.0},
                 why={"artist1": ["Artist0"], "new artist": ["Artist0"]},
                 names={"artist1": "Artist1", "new artist": "New Artist"},
                 recs=[("New Artist", "Fresh Song", 0.7), ("Artist1", "Song1", 0.75)], known={"artist0", "artist1"})


def discover_shell(env, presses):
    shell, fake, out = keyed_shell(env, presses)
    built, played = [], []
    shell.act.recommend = lambda: (built.append(1) or fake_taste())
    shell.act.preview_file = lambda t: played.append(("file", t.artist, t.title))
    shell.act.preview_song = lambda a, t: played.append(("song", a, t)) or True
    shell.act.stop_preview = lambda: played.append(("stop",))
    return shell, fake, out, built, played


def test_discover_shows_the_graph_previews_steers_rebuilds_and_downloads(env):
    cfg, db, *_ = env
    keys_ = ["d",                     # open Discover: the graph tab
             "+",                     # cursor on Artist0 (a seed): more like it
             "2", "enter",            # in your files: play a clip of the best one
             "3", "enter", "d",       # to get: play New Artist - Fresh Song, then download it
             "4", "right", "g",       # adjust: first knob one step further, rebuild
             "q", "q"]
    shell, fake, out, built, played = discover_shell(env, keys_)
    shell.run()
    assert played[0][0] == "file" and played[0][1] == "Artist1"  # top of "in your files": fit 0.8
    assert ("song", "New Artist", "Fresh Song") in played and ("download", "wish1") in fake.calls
    assert len(built) == 2 and played[-1] == ("stop",)  # rebuilt once; leaving stops the clip
    from movescrobble.taste import Tuning

    t = Tuning.load(db)
    assert t.more == ["Artist0"] and t.answer("reach") == "far afield"


def test_discover_blocks_a_track_from_the_list(env):
    cfg, db, *_ = env
    shell, *_ = discover_shell(env, ["d", "3", "b", "q", "q"])
    shell.run()
    assert db.execute("SELECT artist, title FROM blocked").fetchall()[0][:] == ("New Artist", "Fresh Song")


def test_discover_without_lastfm_says_so(env):
    shell, _, out = keyed_shell(env, ["d", "any", "q"])
    shell.run()
    assert "needs Last.fm" in out.getvalue()


def test_discover_graph_tab_draws_a_connected_graph(env):
    from movescrobble.shell import _Discover

    shell, _, out, *_ = discover_shell(env, [])
    d = _Discover(shell)
    shell.console.print(d.render(0))
    screen = out.getvalue()
    assert "◉ you" in screen and "Artist0" in screen and "Artist1" in screen and "New Artist" in screen
    assert any(0x2800 < ord(c) <= 0x28FF for c in screen)  # edges drawn in braille
    assert "one of your artists now" in screen  # side panel of the selected node (Artist0)
    d.tab = 3
    shell.console.print(d.render(0))
    assert "How far from your artists ◂ balanced ▸" in re.sub(r"\s+", " ", out.getvalue())


def test_discover_graph_arrows_move_and_the_panel_plays_an_artists_track(env):
    shell, _, out, built, played = discover_shell(env, ["d", "enter", "down", "enter", "esc", "q", "q"])
    shell.run()
    assert played[0][:2] == ("file", "Artist0")  # Enter on Artist0 opens its tracks, ↓ Enter plays the second


def test_discover_puts_a_pick_first_in_the_playlist_downloading_it(env):
    cfg, db, *_ = env
    staged(env)
    shell, fake, *_ = discover_shell(env, ["d", "3", "f", "q", "q"])
    shell.run()
    first = sorted(json.loads(history.pending_session(db)["manifest"])["tracks"], key=lambda t: t["position"])[0]
    assert ("download", "wish1") in fake.calls and (first["artist"], first["title"]) == ("New Artist", "Fresh Song")
    assert first["source"] == "added"


def test_collage_from_the_menu_picks_a_run_and_any_key_stops_the_editor(env, tmp_path):
    runs = [tmp_path / "newer", tmp_path / "older"]
    for p in runs:
        p.mkdir()
    events = []
    # m = Collage; the run picker: down to the older one, Enter; any key stops the editor; then quit
    shell, _, _ = keyed_shell(env, ["m", "down", "enter", "any", "q"])
    shell.act.collage_runs = lambda: runs
    shell.act.edit_collage = lambda folder: (events.append(folder), (lambda: events.append("stopped"), "http://x"))[1]
    shell.run()
    assert events == [runs[1], "stopped"]  # the screen is cleared between steps, so what was said is gone
