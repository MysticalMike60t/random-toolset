#!/usr/bin/env python3
"""
Pinterest search TUI.

deps: pip install textual httpx

Auth — set ONE of these:

  PINTEREST_TOKEN=<v5 oauth access token>
      Official API (api.pinterest.com/v5). Scoped to YOUR own pins/boards only.
      Needs scopes: pins:read, boards:read.

  PINTEREST_COOKIE="<full Cookie request header>"
      Web session against the internal BaseSearchResource endpoint.
      Searches the global index. Must contain _pinterest_sess and csrftoken.
      Grab it from DevTools > Network > any XHR > Request Headers > cookie.
      Unofficial, rate-limited, ToS-adjacent. Your call.

Keys: enter=search  n=more  s=scope  o=open pin  i=open image  q=quit
"""

from __future__ import annotations

import json
import os
import re
import webbrowser
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Input, Static

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class Result:
    id: str
    title: str
    desc: str
    owner: str
    link: str
    image: str

    @property
    def url(self) -> str:
        return f"https://www.pinterest.com/pin/{self.id}/"


# --------------------------------------------------------------------------- #
# backends
# --------------------------------------------------------------------------- #


class ApiBackend:
    """Official v5 API. Searches the authenticated user's own content."""

    name = "api"
    scopes = ("pins", "boards")

    def __init__(self, token: str):
        self.client = httpx.AsyncClient(
            base_url="https://api.pinterest.com/v5",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )

    async def search(self, query: str, scope: str, bookmark: str | None):
        params: dict[str, str | int] = {"query": query, "page_size": 25}
        if bookmark:
            params["bookmark"] = bookmark
        r = await self.client.get(f"/search/{scope}", params=params)
        r.raise_for_status()
        j = r.json()
        items = j.get("items", [])
        out = [self._board(i) if scope == "boards" else self._pin(i) for i in items]
        return out, j.get("bookmark")

    @staticmethod
    def _pin(p: dict) -> Result:
        media = (p.get("media") or {}).get("images") or {}
        img = ""
        for key in ("1200x", "600x", "400x300", "150x150"):
            if key in media:
                img = media[key].get("url", "")
                break
        return Result(
            id=str(p.get("id", "")),
            title=p.get("title") or p.get("alt_text") or "(untitled)",
            desc=p.get("description") or "",
            owner=(p.get("board_owner") or {}).get("username", ""),
            link=p.get("link") or "",
            image=img,
        )

    @staticmethod
    def _board(b: dict) -> Result:
        return Result(
            id=str(b.get("id", "")),
            title=b.get("name") or "(untitled)",
            desc=b.get("description") or "",
            owner=(b.get("owner") or {}).get("username", ""),
            link=f"https://www.pinterest.com/{(b.get('owner') or {}).get('username', '')}/",
            image=(b.get("media") or {}).get("image_cover_url", ""),
        )

    async def aclose(self):
        await self.client.aclose()


class WebBackend:
    """Internal /resource/BaseSearchResource/get/ endpoint. Global index."""

    name = "web"
    scopes = ("pins", "boards", "users")

    def __init__(self, cookie: str):
        self.cookie = cookie.strip()
        m = re.search(r"csrftoken=([^;\s]+)", self.cookie)
        self.csrf = m.group(1) if m else ""
        self.client = httpx.AsyncClient(timeout=20, follow_redirects=True)

    async def search(self, query: str, scope: str, bookmark: str | None):
        src = f"/search/{scope}/?q={quote(query)}&rs=typed"
        data = {
            "options": {
                "query": query,
                "scope": scope,
                "bookmarks": [bookmark] if bookmark else [],
                "page_size": 25,
                "no_fetch_context_on_resource": False,
            },
            "context": {},
        }
        headers = {
            "cookie": self.cookie,
            "x-csrftoken": self.csrf,
            "x-requested-with": "XMLHttpRequest",
            "x-app-version": "cb1b3d9",
            "x-pinterest-appstate": "active",
            "x-pinterest-pws-handler": f"www/search/[{scope}].js",
            "accept": "application/json, text/javascript, */*, q=0.01",
            "referer": f"https://www.pinterest.com{src}",
            "user-agent": UA,
        }
        r = await self.client.get(
            "https://www.pinterest.com/resource/BaseSearchResource/get/",
            params={"source_url": src, "data": json.dumps(data, separators=(",", ":"))},
            headers=headers,
        )
        r.raise_for_status()
        rr = r.json().get("resource_response", {})
        raw = rr.get("data") or []
        if isinstance(raw, dict):
            raw = raw.get("results", [])
        out = [self._map(o, scope) for o in raw if self._keep(o, scope)]
        return out, rr.get("bookmark")

    @staticmethod
    def _keep(o: dict, scope: str) -> bool:
        t = o.get("type")
        return t in {"pins": {"pin"}, "boards": {"board"}, "users": {"user"}}[scope]

    @staticmethod
    def _map(o: dict, scope: str) -> Result:
        if scope == "pins":
            imgs = o.get("images") or {}
            img = (imgs.get("orig") or imgs.get("736x") or {}).get("url", "")
            return Result(
                id=str(o.get("id", "")),
                title=o.get("grid_title") or o.get("title") or "(untitled)",
                desc=o.get("description") or "",
                owner=(o.get("pinner") or {}).get("username", ""),
                link=o.get("link") or "",
                image=img,
            )
        if scope == "boards":
            owner = (o.get("owner") or {}).get("username", "")
            return Result(
                id=str(o.get("id", "")),
                title=o.get("name") or "(untitled)",
                desc=f"{o.get('pin_count', 0)} pins",
                owner=owner,
                link="https://www.pinterest.com" + (o.get("url") or ""),
                image=(o.get("image_cover_url") or ""),
            )
        return Result(
            id=str(o.get("id", "")),
            title=o.get("full_name") or o.get("username") or "(unnamed)",
            desc=o.get("about") or "",
            owner=o.get("username", ""),
            link=f"https://www.pinterest.com/{o.get('username', '')}/",
            image=o.get("image_large_url", ""),
        )

    async def aclose(self):
        await self.client.aclose()


def make_backend():
    tok = os.getenv("PINTEREST_TOKEN")
    if tok:
        return ApiBackend(tok)
    ck = os.getenv("PINTEREST_COOKIE")
    if ck:
        return WebBackend(ck)
    raise SystemExit("set PINTEREST_TOKEN or PINTEREST_COOKIE")


# --------------------------------------------------------------------------- #
# tui
# --------------------------------------------------------------------------- #


class PinSearch(App):
    CSS = """
    Input { dock: top; }
    #body { height: 1fr; }
    #results { width: 2fr; }
    #detail { width: 1fr; padding: 1 2; border-left: solid $accent; overflow-y: auto; }
    """

    BINDINGS = [
        ("n", "next_page", "More"),
        ("s", "cycle_scope", "Scope"),
        ("o", "open_pin", "Open"),
        ("i", "open_image", "Image"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.backend = make_backend()
        self.scope = "pins"
        self.query = ""
        self.bookmark: str | None = None
        self.results: list[Result] = []

    def compose(self) -> ComposeResult:
        yield Input(placeholder="search…", id="q")
        with Horizontal(id="body"):
            yield DataTable(id="results")
            yield Static("", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        t.cursor_type = "row"
        t.zebra_stripes = True
        t.add_columns("#", "title", "owner", "description")
        self.query_one(Input).focus()
        self._title()

    def _title(self) -> None:
        self.title = (
            f"pinsearch [{self.backend.name}:{self.scope}] {len(self.results)} results"
        )

    # ---- actions ----

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query = event.value.strip()
        if not self.query:
            return
        self.bookmark = None
        self.results.clear()
        self.query_one(DataTable).clear()
        self.run_search()

    def action_next_page(self) -> None:
        if self.query and self.bookmark:
            self.run_search()

    def action_cycle_scope(self) -> None:
        s = self.backend.scopes
        self.scope = s[(s.index(self.scope) + 1) % len(s)]
        self._title()
        if self.query:
            self.bookmark = None
            self.results.clear()
            self.query_one(DataTable).clear()
            self.run_search()

    def _current(self) -> Result | None:
        t = self.query_one(DataTable)
        if 0 <= t.cursor_row < len(self.results):
            return self.results[t.cursor_row]
        return None

    def action_open_pin(self) -> None:
        r = self._current()
        if r:
            webbrowser.open(r.url if self.scope == "pins" else r.link)

    def action_open_image(self) -> None:
        r = self._current()
        if r and r.image:
            webbrowser.open(r.image)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if 0 <= event.cursor_row < len(self.results):
            r = self.results[event.cursor_row]
            self.query_one("#detail", Static).update(
                f"[b]{r.title}[/b]\n\n"
                f"[dim]id[/dim]     {r.id}\n"
                f"[dim]owner[/dim]  {r.owner}\n"
                f"[dim]pin[/dim]    {r.url if self.scope == 'pins' else '-'}\n"
                f"[dim]link[/dim]   {r.link or '-'}\n"
                f"[dim]image[/dim]  {r.image or '-'}\n\n"
                f"{r.desc}"
            )

    # ---- worker ----

    @work(exclusive=True)
    async def run_search(self) -> None:
        self.query_one("#detail", Static).update("searching…")
        try:
            items, bm = await self.backend.search(self.query, self.scope, self.bookmark)
        except httpx.HTTPStatusError as e:
            self.query_one("#detail", Static).update(
                f"[red]HTTP {e.response.status_code}[/red]\n\n{e.response.text[:800]}"
            )
            return
        except Exception as e:  # noqa: BLE001
            self.query_one("#detail", Static).update(
                f"[red]{type(e).__name__}[/red] {e}"
            )
            return

        self.bookmark = bm
        t = self.query_one(DataTable)
        for r in items:
            self.results.append(r)
            t.add_row(
                str(len(self.results)),
                r.title[:60],
                r.owner[:20],
                r.desc.replace("\n", " ")[:80],
            )
        self._title()
        if not items:
            self.query_one("#detail", Static).update("no results")

    async def on_unmount(self) -> None:
        await self.backend.aclose()


if __name__ == "__main__":
    PinSearch().run()
