"""Keyboard-driven widgets: menu, single choice, checklist, yes/no.

Arrows or j/k move, Enter picks, Esc/q cancels, and every item with a shortcut letter is one key away.
Widgets take `read_key` so tests can feed a scripted key sequence.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from . import keys as keymod


@dataclass(frozen=True)
class Item:
    key: str  # shortcut letter ("" = none) and the value returned
    label: str
    hint: str = ""


class Busy:
    """A spinner with a status line for slow work. `update` may be called from anywhere (worker threads too) and is
    a no-op while nothing is running, so it can be handed to code that also runs headless (the launchd agent)."""

    def __init__(self, console: Console):
        self.console = console
        self._status = None

    @contextmanager
    def __call__(self, message: str) -> Iterator[None]:
        if self._status is not None:  # nested: the outer spinner shows the inner step
            self.update(message)
            yield
            return
        if not self.console.is_terminal:  # pipes and tests: no animation, just say what is happening
            self.console.print(f"[dim]{message} ...[/]")
            yield
            return
        with self.console.status(message, spinner="dots") as status:
            self._status = status
            try:
                yield
            finally:
                self._status = None

    def update(self, message: str) -> None:
        if self._status is not None:
            self._status.update(message)


class KeyUI:
    def __init__(self, console: Console, read_key: Callable[[], str] | None = None, raw: Callable | None = None):
        self.console = console
        self._read = read_key or keymod.read_key
        self._raw = raw or keymod.raw_mode

    # -- core loop --------------------------------------------------------------------------------
    def _loop(self, render: Callable[[int], Group | Text], n: int, on_key: Callable[[str, int], tuple[int, object]],
              start: int = 0, on_move: Callable[[int, int], None] | None = None, vim: bool = True,
              arrows: bool = True):
        """Redraw after every key. on_key(key, index) -> (new_index, result); result not None ends the loop.
        `arrows=False` hands the arrow keys to on_key too (for screens that are not a list)."""
        idx = max(0, min(start, n - 1)) if n else 0
        with self._raw(), Live(render(idx), console=self.console, auto_refresh=False, transient=True) as live:
            while True:
                key = self._read()
                before = idx
                if not arrows:
                    idx, result = on_key(key, idx)
                    if result is not None:
                        return result
                elif key == "up" or (vim and key == "k"):
                    idx = (idx - 1) % n if n else 0
                elif key == "down" or (vim and key == "j"):
                    idx = (idx + 1) % n if n else 0
                elif key == "home":
                    idx = 0
                elif key == "end":
                    idx = max(n - 1, 0)
                else:
                    idx, result = on_key(key, idx)
                    if result is not None:
                        return result
                if on_move and idx != before and key in ("up", "down", "k", "j", "home", "end"):
                    on_move(before, idx)
                live.update(render(idx), refresh=True)

    @staticmethod
    def _line(selected: bool, text: str, hint: str = "", mark: str = "") -> Text:
        t = Text()
        t.append("▸ " if selected else "  ", style="bold cyan")
        if mark:
            t.append(mark + " ", style="green")
        t.append(text, style="bold reverse" if selected else "")
        if hint:
            t.append(f"  {hint}", style="dim")
        return t

    # -- widgets ----------------------------------------------------------------------------------
    def menu(self, items: list[Item], title: str = "", footer: str = "", start: int = 0) -> str | None:
        """Returns the chosen item's key, or None for Esc/q."""
        def render(idx):
            rows = [Text(title, style="bold")] if title else []
            for i, it in enumerate(items):
                rows.append(Text.assemble(("▸ " if i == idx else "  ", "bold cyan"),
                                          (f"{it.key or ' '}  ", "bold yellow"),
                                          (it.label, "bold reverse" if i == idx else ""),
                                          (f"  {it.hint}" if it.hint else "", "dim")))
            rows.append(Text(footer or "↑/↓ move · Enter select · shortcut letters · q quit", style="dim"))
            return Group(*rows)

        def on_key(key, idx):
            if key == "enter":
                return idx, items[idx].key or str(idx)
            if key == "esc" or (key == "q" and not any(it.key == "q" for it in items)):
                return idx, "__cancel__"
            for i, it in enumerate(items):
                if it.key and key == it.key:
                    return i, it.key
            return idx, None

        vim = not any(it.key in ("j", "k") for it in items)  # a shortcut letter beats vim-style movement
        r = self._loop(render, len(items), on_key, start, vim=vim)
        return None if r == "__cancel__" else r

    def select(self, rows: list[str], title: str = "", extra: list[Item] | None = None) -> int | str | None:
        """Pick one row (returns its index) or one of the `extra` items (returns its key); None = cancelled."""
        extra = extra or []
        n = len(rows) + len(extra)

        def render(idx):
            out = [Text(title, style="bold")] if title else []
            for i, r in enumerate(rows):
                out.append(self._line(i == idx, r))
            for j, e in enumerate(extra):
                out.append(self._line(len(rows) + j == idx, e.label, e.hint))
            out.append(Text("↑/↓ move · Enter select · Esc cancel", style="dim"))
            return Group(*out)

        def on_key(key, idx):
            if key == "enter":
                return idx, (idx if idx < len(rows) else extra[idx - len(rows)].key)
            if key == "esc":
                return idx, "__cancel__"
            for e in extra:
                if e.key and key == e.key:
                    return idx, e.key
            return idx, None

        r = self._loop(render, n, on_key)
        return None if r == "__cancel__" else r

    def multiselect(self, rows: list[str], title: str = "", allow_none: bool = False) -> list[int] | None:
        """Checklist: Space toggles, a = all/none, Enter confirms (returns checked indexes), Esc cancels.
        Enter with nothing ticked acts on the highlighted row, or returns [] with `allow_none`."""
        checked: set[int] = set()

        def render(idx):
            out = [Text(title, style="bold")] if title else []
            for i, r in enumerate(rows):
                out.append(self._line(i == idx, r, mark="[x]" if i in checked else "[ ]"))
            out.append(Text(f"↑/↓ move · Space tick · a all/none · Enter confirm ({len(checked)} ticked) · Esc cancel",
                            style="dim"))
            return Group(*out)

        def on_key(key, idx):
            if key == "space":
                checked.symmetric_difference_update({idx})
            elif key == "a":
                checked.clear() if len(checked) == len(rows) else checked.update(range(len(rows)))
            elif key == "enter":
                return idx, sorted(checked) if checked or allow_none else [idx]
            elif key == "esc":
                return idx, "__cancel__"
            return idx, None

        r = self._loop(render, len(rows), on_key)
        return None if r == "__cancel__" else r

    def confirm(self, question: str, default: bool = False) -> bool:
        items = [Item("y", "Yes"), Item("n", "No")]
        r = self.menu(items, title=question, footer="y / n · Enter = " + ("Yes" if default else "No"), start=0 if default else 1)
        return r == "y"

    def pause(self, message: str = "press any key") -> None:
        self.console.print(f"[dim]{message}[/]")
        with self._raw():
            self._read()
