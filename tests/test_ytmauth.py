import inspect
import json
import stat
from types import SimpleNamespace

import pytest

from movescrobble import cli, ytmauth


def cookie(name, value="v", domain=".youtube.com"):
    return SimpleNamespace(name=name, value=value, domain=domain)


SIGNED_IN = [cookie("__Secure-3PAPISID", "abc"), cookie("SID", "def"), cookie("SID", "other", ".google.com"),
             cookie("bank", "secret", ".example.com")]


def test_only_youtube_cookies_are_kept():
    headers = ytmauth.headers_from(SIGNED_IN)
    assert "__Secure-3PAPISID=abc" in headers["cookie"] and "SID=def" in headers["cookie"]
    assert "other" not in headers["cookie"] and "bank" not in headers["cookie"]


def test_headers_are_what_ytmusicapi_treats_as_a_browser_login():
    from ytmusicapi import YTMusic
    from ytmusicapi.auth.types import AuthType

    assert YTMusic(ytmauth.headers_from(SIGNED_IN)).auth_type == AuthType.BROWSER


def test_second_google_account_can_be_chosen():
    assert ytmauth.headers_from(SIGNED_IN, 2)["x-goog-authuser"] == "2"


@pytest.mark.parametrize("cookies", [[], [cookie("YSC")], [cookie("__Secure-3PAPISID", domain=".google.com")]])
def test_a_browser_that_is_not_signed_in_is_told_plainly(cookies):
    with pytest.raises(ytmauth.LoginError, match="sign in at https://music.youtube.com"):
        ytmauth.headers_from(cookies)


def test_saved_file_is_private_even_over_an_open_old_one(tmp_path):
    path = tmp_path / "sub" / "auth.json"
    path.parent.mkdir()
    path.write_text("old")
    path.chmod(0o644)
    ytmauth.save(path, {"cookie": "x"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and json.loads(path.read_text()) == {"cookie": "x"}


def test_rejected_login_says_so():
    def refuses(_headers):
        raise RuntimeError("401")

    with pytest.raises(ytmauth.LoginError, match="did not accept that login: sign in again.*401"):
        ytmauth.account_name({}, refuses)


def test_account_name_comes_from_a_read_only_call():
    client = lambda _h: SimpleNamespace(get_account_info=lambda: {"accountName": "Me"})  # noqa: E731
    assert ytmauth.account_name({}, client) == "Me"


def test_unreadable_cookies_file_is_a_login_error(tmp_path):
    with pytest.raises(ytmauth.LoginError):
        ytmauth.read_file(tmp_path / "missing.txt")


def test_cookies_txt_is_read(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\t__Secure-3PAPISID\tabc\n")
    assert "__Secure-3PAPISID=abc" in ytmauth.headers_from(ytmauth.read_file(f))["cookie"]


def test_the_yt_dlp_call_matches_its_real_signature():
    from yt_dlp.cookies import SUPPORTED_BROWSERS, extract_cookies_from_browser

    assert list(inspect.signature(extract_cookies_from_browser).parameters)[:2] == ["browser_name", "profile"]
    assert set(ytmauth.BROWSERS) <= set(SUPPORTED_BROWSERS)


class Env:
    def __init__(self, monkeypatch, tmp_path, browsers=("firefox",), asks=()):
        self.path = tmp_path / "auth.json"
        self.read = []
        self.asks = list(asks)
        monkeypatch.setattr(cli.cfgmod, "load", lambda: SimpleNamespace(ytm_auth_file_path=self.path))
        monkeypatch.setattr(ytmauth, "installed", lambda: list(browsers))
        monkeypatch.setattr(ytmauth, "read_browser", lambda b, p=None: (self.read.append((b, p)) or SIGNED_IN))
        monkeypatch.setattr(ytmauth, "account_name", lambda h: "Me")
        monkeypatch.setattr("builtins.input", lambda _prompt="": self.asks.pop(0))


def args(browser=None, cookies=None, account=0, profile=None):
    return SimpleNamespace(browser=browser, cookies=cookies, account=account, profile=profile)


def test_login_with_one_browser_needs_no_questions(monkeypatch, tmp_path, capsys):
    env = Env(monkeypatch, tmp_path)
    cli.cmd_ytm_auth(args())
    out = capsys.readouterr().out
    assert env.read == [("firefox", None)] and "Signed in as Me" in out and env.path.exists()
    assert "abc" not in out  # nothing secret on the screen


def test_several_browsers_are_offered_by_number(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path, ("firefox", "chrome"), ["2"])
    cli.cmd_ytm_auth(args())
    assert env.read[0][0] == "chrome"


def test_a_failed_login_saves_nothing(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path)

    def refuses(_headers):
        raise ytmauth.LoginError("YouTube Music did not accept that login: 401")

    monkeypatch.setattr(ytmauth, "account_name", refuses)
    with pytest.raises(SystemExit, match="did not accept"):
        cli.cmd_ytm_auth(args())
    assert not env.path.exists()


def test_no_supported_browser_points_to_the_file_route(monkeypatch, tmp_path):
    Env(monkeypatch, tmp_path, ())
    with pytest.raises(SystemExit, match="--cookies"):
        cli.cmd_ytm_auth(args())


def test_a_cookies_file_needs_no_browser(monkeypatch, tmp_path):
    env = Env(monkeypatch, tmp_path)
    f = tmp_path / "c.txt"
    f.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\t__Secure-3PAPISID\tabc\n")
    cli.cmd_ytm_auth(args(cookies=str(f)))
    assert env.read == [] and env.path.exists()


def test_saved_login_loads_the_way_the_playlist_code_opens_it(tmp_path):
    from ytmusicapi import YTMusic
    from ytmusicapi.auth.types import AuthType

    path = tmp_path / "auth.json"
    ytmauth.save(path, ytmauth.headers_from(SIGNED_IN))
    assert YTMusic(str(path)).auth_type == AuthType.BROWSER
