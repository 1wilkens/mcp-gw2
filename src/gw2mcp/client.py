"""Async client for the Guild Wars 2 API (https://api.guildwars2.com/v2)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Sequence
from typing import Any

import httpx

from gw2mcp import __version__

log = logging.getLogger(__name__)

MAX_IDS_PER_REQUEST = 200
_PATH_RE = re.compile(r"^v2(/[^/?#\s][^/?#]*)*$")

# Top-level v2 routes reachable through the raw ``api_get`` tool. This is an allowlist so that
# credential endpoints (``createsubtoken``, ``tokeninfo``) and future unknown routes stay blocked.
RAW_API_ROOTS: frozenset[str] = frozenset(
    {
        "account",
        "achievements",
        "adventures",
        "backstory",
        "build",
        "characters",
        "colors",
        "commerce",
        "continents",
        "currencies",
        "dailycrafting",
        "dungeons",
        "emblem",
        "emotes",
        "events",
        "events-state",
        "files",
        "finishers",
        "gemstore",
        "gliders",
        "guild",
        "home",
        "homestead",
        "items",
        "itemstats",
        "jadebots",
        "legendaryarmory",
        "legends",
        "logos",
        "mailcarriers",
        "mapchests",
        "maps",
        "masteries",
        "materials",
        "minis",
        "mounts",
        "novelties",
        "outfits",
        "pets",
        "professions",
        "pvp",
        "quaggans",
        "quests",
        "races",
        "raids",
        "recipes",
        "skiffs",
        "skills",
        "skins",
        "specializations",
        "stories",
        "titles",
        "traits",
        "vendors",
        "wizardsvault",
        "worldbosses",
        "worlds",
        "wvw",
    }
)


class GW2APIError(Exception):
    """An error response from the GW2 API."""

    def __init__(self, status: int, message: str, path: str = "") -> None:
        super().__init__(f"GW2 API error {status} for {path or '?'}: {message}")
        self.status = status
        self.message = message
        self.path = path


class GW2AuthError(GW2APIError):
    """The API key is missing, invalid or lacks a required permission."""


class MissingAPIKeyError(GW2AuthError):
    def __init__(self, path: str = "") -> None:
        super().__init__(401, "no API key configured (set GW2_API_KEY or GW2_API_KEY_FILE)", path)


def normalize_path(path: str) -> str:
    """Validate and normalize an API path such as ``/v2/items`` or ``v2/account/bank``."""
    p = path.strip().lstrip("/")
    # percent-encoding and backslashes could smuggle traversal past these checks; httpx encodes paths itself
    if any(token in p for token in ("://", "..", "?", "#", "%", "\\")):
        raise ValueError(f"invalid API path {path!r}")
    if not p.startswith("v2"):
        p = "v2/" + p
    p = p.rstrip("/")
    if not _PATH_RE.match(p):
        raise ValueError(f"invalid API path {path!r}")
    return p


def normalize_raw_path(path: str) -> str:
    """Normalize a caller-supplied path for raw access, allowing only known non-credential routes."""
    p = normalize_path(path)
    root = p.split("/")[1] if "/" in p else None
    if root is not None and root not in RAW_API_ROOTS:
        raise ValueError(f"API path {path!r} is not available through api_get")
    return p


def chunked(seq: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


class GW2Client:
    """Thin async wrapper around the GW2 v2 API with retries, chunking and paging."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        lang: str = "en",
        base_url: str = "https://api.guildwars2.com",
        schema_version: str = "latest",
        transport: httpx.AsyncBaseTransport | None = None,
        max_concurrency: int = 8,
        max_retries: int = 4,
        timeout: float = 30.0,
        backoff: float = 1.0,
    ) -> None:
        self.api_key = api_key
        self.lang = lang
        self.max_retries = max_retries
        self.backoff = backoff
        self._sem = asyncio.Semaphore(max_concurrency)
        self._http = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            headers={
                "User-Agent": f"gw2mcp/{__version__}",
                "X-Schema-Version": schema_version,
                "Accept": "application/json",
            },
        )

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> GW2Client:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def request(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = False) -> httpx.Response:
        """Perform a GET request and return the raw response (raising on errors)."""
        path = normalize_path(path)
        headers: dict[str, str] = {}
        if auth and not self.api_key:
            raise MissingAPIKeyError(path)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        query = {k: v for k, v in (params or {}).items() if v is not None}
        query.setdefault("lang", self.lang)

        attempt = 0
        while True:
            async with self._sem:
                try:
                    resp = await self._http.get("/" + path, params=query, headers=headers)
                except httpx.TransportError as exc:
                    if attempt >= self.max_retries:
                        raise GW2APIError(0, f"network error: {exc}", path) from exc
                    resp = None
            if resp is not None and resp.status_code < 400:
                return resp
            retryable = resp is None or resp.status_code in (429, 500, 502, 503, 504)
            if retryable and attempt < self.max_retries:
                delay = self.backoff * (2**attempt)
                if resp is not None and resp.headers.get("Retry-After", "").isdigit():
                    delay = max(delay, float(resp.headers["Retry-After"]))
                log.debug("retrying %s in %.1fs (status %s)", path, delay, resp and resp.status_code)
                attempt += 1
                await asyncio.sleep(delay)
                continue
            assert resp is not None
            message = _error_text(resp)
            if resp.status_code in (401, 403):
                raise GW2AuthError(resp.status_code, message, path)
            raise GW2APIError(resp.status_code, message, path)

    async def get(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = False) -> Any:
        resp = await self.request(path, params, auth=auth)
        return resp.json()

    async def get_many(self, path: str, ids: Iterable[int | str], *, auth: bool = False) -> list[dict[str, Any]]:
        """Fetch objects for many ids, chunked in batches of 200. Unknown ids are skipped."""
        unique = list(dict.fromkeys(str(i) for i in ids))
        if not unique:
            return []

        async def fetch(chunk: Sequence[str]) -> list[dict[str, Any]]:
            try:
                data = await self.get(path, {"ids": ",".join(chunk)}, auth=auth)
            except GW2APIError as exc:
                if exc.status == 404:  # "all ids provided are invalid"
                    return []
                raise
            return data if isinstance(data, list) else []

        results = await asyncio.gather(*(fetch(c) for c in chunked(unique, MAX_IDS_PER_REQUEST)))
        return [obj for chunk in results for obj in chunk]

    async def get_all_pages(self, path: str, *, page_size: int = 200, auth: bool = False) -> list[Any]:
        """Fetch all objects of a bulk endpoint using pagination."""
        first = await self.request(path, {"page": 0, "page_size": page_size}, auth=auth)
        items: list[Any] = list(first.json())
        try:
            total_pages = int(first.headers.get("X-Page-Total", "1"))
        except ValueError:
            total_pages = 1
        if total_pages > 1:
            pages = await asyncio.gather(
                *(self.get(path, {"page": p, "page_size": page_size}, auth=auth) for p in range(1, total_pages))
            )
            for page in pages:
                items.extend(page)
        return items


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            for key in ("text", "error"):
                if key in data:
                    return str(data[key])
    except ValueError:
        pass
    return resp.text[:200] or resp.reason_phrase
