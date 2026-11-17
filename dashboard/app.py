"""The dashboard's web server: a handful of read-only addresses and the page itself.

Every address answers GET and nothing else. There is no address that places
an order, changes a limit, starts a run or writes anything at all, so the
page cannot do those things whatever is clicked on it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import anyio
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import config
from dashboard import personas
from dashboard.reader import NoJournal, Reader

STATIC = Path(__file__).parent / "static"
NO_STORE = {"Cache-Control": "no-store"}
EVERY_ADDRESS = {"0.0.0.0", "::"}
HOST_HEADER = re.compile(r"^(?:\[([0-9a-f:.]+)\]|([a-z0-9.\-]+))(?::\d+)?$")


class PageFiles(StaticFiles):
    """The page's own files. The browser is told to check each one against the disk
    before reusing its copy, so an updated dashboard is never shown with old scripts."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


async def _pause(seconds: float) -> None:
    """The wait between two looks at the journal. Its own function so a test can stand in for it."""
    await anyio.sleep(seconds)


def names_answered(host: str) -> set[str] | None:
    """The names a browser may use for this server. None means any name.

    The page has no login, so the one thing between a web site and the
    account is that the site cannot reach this computer under its own name.
    A request that names any other host is therefore refused: that stops a
    page on some other site from pointing its own name at this computer and
    reading the records through the visitor's browser. Listening on every
    address is the owner's decision to open the page up, and then any name
    is answered.
    """
    if host in EVERY_ADDRESS:
        return None
    return {"127.0.0.1", "localhost", "::1", host.lower()}


def host_named(header: str) -> str:
    """The host a browser addressed, without the port. An IPv6 address comes in brackets: [::1]:8765.

    Empty for anything that is not a plain name or address with at most a port after it.
    """
    match = HOST_HEADER.match(header.strip().lower())
    return (match.group(1) or match.group(2)) if match else ""


class OnlyNamed:
    """Turns away every request that does not address this server by one of its own names."""

    def __init__(self, app, names: set[str] | None) -> None:
        self.app, self.names = app, names

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and self.names is not None:
            header = dict(scope["headers"]).get(b"host", b"").decode("latin-1")
            if host_named(header) not in self.names:
                refusal = PlainTextResponse("The dashboard does not answer to this address.", status_code=400)
                await refusal(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(
    journal_path: str | Path | None = None, evaluation_dir: str | Path | None = None, poll_seconds: float = 1.0,
    host: str | None = None,
) -> Starlette:
    reader = Reader(journal_path, evaluation_dir)

    def reply(data: dict, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status, headers=NO_STORE)

    def arm_of(request: Request) -> str | None:
        """The arm asked for, or the first one in the journal when none is named. None when there is none."""
        known = reader.arms()
        wanted = request.query_params.get("arm")
        if wanted:
            return wanted if wanted in known else None
        return known[0] if known else None

    async def answered(read) -> JSONResponse:
        """Do the reading off the server's main thread, so one slow read never holds up the other pages
        and the live streams, and turn a journal that is missing or unreadable into a plain answer."""
        try:
            return reply(*await anyio.to_thread.run_sync(read))
        except NoJournal:
            return reply({"error": "还没有账本：每日流程还没有运行过。", "journal": False}, 404)
        except sqlite3.Error as error:
            return reply({"error": f"账本读不了：{error}"}, 500)

    def page(build):
        """An address that shows one arm's data."""
        async def handler(request: Request) -> JSONResponse:
            def read() -> tuple[dict, int]:
                arm = arm_of(request)
                if arm is None:
                    return {"error": "账本里没有这套模型配置的记录。"}, 404
                return build(arm, request), 200
            return await answered(read)
        return handler

    async def roster(request: Request) -> JSONResponse:
        return reply({
            "roster": personas.roster(), "narrator": personas.NARRATOR,
            "stages": [{"id": key, "title": title} for key, title in personas.STAGES],
        })

    async def arms(request: Request) -> JSONResponse:
        def read() -> tuple[dict, int]:
            try:
                return {"journal": True, "arms": reader.arms(), "path": str(reader.journal_path)}, 200
            except NoJournal:
                return {"journal": False, "arms": [], "path": str(reader.journal_path)}, 200
        return await answered(read)

    async def stream(request: Request) -> EventSourceResponse:
        """New events as they are recorded, for as long as the page stays connected.

        The page says where it stands (`run_id`, `seq`, `status`), so it is
        sent only what it has not seen. `polls` ends the stream after that
        many looks: a way to test it, of no use to the page.
        """
        params = request.query_params

        def whole(name: str) -> int | None:
            text = params.get(name) or ""
            return int(text) if text.isdigit() else None

        arm, run_id = params.get("arm"), params.get("run_id") or None
        seq, status = whole("seq") or 0, params.get("status") or None
        polls = whole("polls")

        def look() -> list[dict]:
            nonlocal run_id, seq, status
            wanted = arm or (reader.arms() or [None])[0]
            if wanted is None:
                return []
            messages, run_id, seq, status = reader.tail(wanted, run_id, seq, status)
            return messages

        async def events():
            looked = 0
            while polls is None or looked < polls:
                if await request.is_disconnected():
                    break
                try:
                    for message in await anyio.to_thread.run_sync(look):
                        yield {"event": message["type"], "data": json.dumps(message, ensure_ascii=False)}
                except (NoJournal, sqlite3.Error):
                    pass                                # nothing to read yet, or not readable just now: look again
                looked += 1
                await _pause(poll_seconds)

        return EventSourceResponse(events(), headers=NO_STORE)

    def meeting(arm: str, request: Request) -> dict:
        params = request.query_params
        return reader.meeting(arm, params.get("session") or None, params.get("run_id") or None)

    routes = [
        Route("/api/roster", roster, methods=["GET"]),
        Route("/api/arms", arms, methods=["GET"]),
        Route("/api/sessions", page(lambda arm, request: {"arm": arm, "sessions": reader.sessions(arm)}), methods=["GET"]),
        Route("/api/meeting", page(meeting), methods=["GET"]),
        Route("/api/overview", page(lambda arm, request: reader.overview(arm)), methods=["GET"]),
        Route("/api/book", page(lambda arm, request: reader.book(arm)), methods=["GET"]),
        Route("/api/evaluation", page(lambda arm, request: reader.evaluation(arm)), methods=["GET"]),
        Route("/api/status", page(lambda arm, request: reader.status(arm)), methods=["GET"]),
        Route("/api/stream", stream, methods=["GET"]),
        Mount("/", PageFiles(directory=STATIC, html=True)),
    ]
    return Starlette(routes=routes, middleware=[Middleware(OnlyNamed, names=names_answered(host or config.DASHBOARD_HOST))])
