import shutil

import pytest


def pytest_collection_modifyitems(config, items):
    """Tests marked `ffmpeg` run the real encoder; skip them where it is not installed."""
    if shutil.which("ffmpeg"):
        return
    skip = pytest.mark.skip(reason="ffmpeg not installed")
    for item in items:
        if item.get_closest_marker("ffmpeg"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def no_real_youtube(monkeypatch):
    """Config's auth file is in your real home: no test may reach your YouTube account through it."""
    monkeypatch.setattr("movescrobble.ytmirror.open_client", lambda cfg: None)
