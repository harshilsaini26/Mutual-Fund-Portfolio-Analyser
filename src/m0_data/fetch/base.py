"""Fetch layer. MODULE_0.md §5, with §2.3's scraping hygiene.

Everything that touches the network lives here, so the parser below it is a
pure function on text and every test in this module runs offline.

The one rule that is not a preference: **write the file to disk before
inserting the manifest row** (§5.2). A `raw_file` row pointing at a file that
does not exist is unrecoverable — the bytes are gone and nothing can re-derive
them. An orphan file on disk is harmless and a reconcile job finds it.
"""

from __future__ import annotations

import hashlib
import random
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

#: Extension by content type, for the archive path. Unknown types keep `.bin`
#: rather than guessing: the manifest records the real content_type anyway.
_EXTENSIONS = {
    "text/plain": "txt",
    "text/csv": "csv",
    "application/json": "json",
    "application/pdf": "pdf",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/zip": "zip",
}


class FetchError(RuntimeError):
    """The fetch failed after retries. §2 requires the URL be logged, not guessed at."""


@dataclass(frozen=True)
class FetchCandidate:
    """MODULE_0.md §5.1."""

    url: str
    source_id: str
    as_of_hint: Any = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchResult:
    """`file_id` is None only when nothing was archived (304, or failure)."""

    file_id: str | None
    url: str
    status: str  # fetched|unchanged|failed
    byte_size: int = 0
    error: str | None = None
    storage_path: str | None = None


class SourceFetcher(Protocol):
    """MODULE_0.md §5.1."""

    source_id: str

    def discover(self, as_of: Any) -> list[FetchCandidate]:
        """Candidate URLs. One item for a fixed-URL source."""
        ...

    def fetch(self, candidate: FetchCandidate) -> FetchResult: ...


class DomainRateLimiter:
    """Token bucket, per netloc. MODULE_0.md §5.3.

    Per-domain rather than global: two sources on different hosts should not
    queue behind each other, and one host must not be hammered because another
    was idle. Shared across every fetcher in a process.
    """

    def __init__(self, rate_per_sec: float = 0.5, burst: int = 2) -> None:
        self.rate = rate_per_sec
        self.burst = burst
        self._tokens: dict[str, float] = {}
        self._last: dict[str, float] = {}

    def acquire(self, url: str, sleep: Any = time.sleep) -> float:
        """Block until a token is available. Returns the seconds waited."""
        host = urlparse(url).netloc
        now = time.monotonic()
        last = self._last.get(host, now)
        tokens = min(self.burst, self._tokens.get(host, float(self.burst)))
        tokens += (now - last) * self.rate
        tokens = min(tokens, float(self.burst))

        waited = 0.0
        if tokens < 1.0:
            waited = (1.0 - tokens) / self.rate
            sleep(waited)
            tokens = 1.0
            now += waited

        self._tokens[host] = tokens - 1.0
        self._last[host] = now
        return waited


class RobotsCache:
    """robots.txt per domain, cached. MODULE_0.md §2.3 calls this mandatory.

    A domain that will not serve robots.txt is treated as permitting the
    fetch: an unreachable policy file is not a prohibition, and refusing on
    that basis would make the job fail for a reason unrelated to the data.
    """

    def __init__(self, ttl_seconds: float = 7 * 24 * 3600) -> None:
        self.ttl = ttl_seconds
        self._parsers: dict[str, tuple[float, urllib.robotparser.RobotFileParser]] = {}

    def allows(self, url: str, user_agent: str, client: Any = None) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._parsers.get(origin)
        if cached is None or (time.monotonic() - cached[0]) > self.ttl:
            parser = urllib.robotparser.RobotFileParser()
            try:
                getter = client or httpx
                response = getter.get(f"{origin}/robots.txt", timeout=15)
                parser.parse(response.text.splitlines())
            except Exception:
                # An unreachable policy file is not a prohibition.
                parser.allow_all = True  # type: ignore[attr-defined]
            self._parsers[origin] = (time.monotonic(), parser)
            cached = self._parsers[origin]
        return bool(cached[1].can_fetch(user_agent, url))


def file_id_for(content: bytes) -> str:
    """Content hash. §5.2 — identity is the bytes, not the URL or the date."""
    return hashlib.sha256(content).hexdigest()


def infer_extension(content_type: str | None, url: str) -> str:
    if content_type:
        base = content_type.split(";")[0].strip().lower()
        if base in _EXTENSIONS:
            return _EXTENSIONS[base]
    suffix = Path(urlparse(url).path).suffix.lstrip(".").lower()
    return suffix if suffix.isalnum() and suffix else "bin"


def archive_path(root: Path, source_id: str, file_id: str, ext: str) -> Path:
    """§3.1: /raw/{source_id}/{yyyy}/{mm}/{sha256[:2]}/{sha256}.{ext}

    The two-character shard keeps any one directory from accumulating tens of
    thousands of entries, which some filesystems handle badly.
    """
    now = datetime.now(UTC)
    return root / source_id / f"{now:%Y}" / f"{now:%m}" / file_id[:2] / f"{file_id}.{ext}"


def archive(
    content: bytes,
    candidate: FetchCandidate,
    content_type: str | None,
    root: Path,
    already_archived: Any,
) -> tuple[FetchResult, Path | None]:
    """Write the bytes, once. MODULE_0.md §5.2.

    Identical bytes already on disk are a no-op — the archive is content
    addressed, so re-fetching an unchanged file costs nothing and creates
    nothing. This is what makes a daily job safe to run repeatedly.
    """
    fid = file_id_for(content)
    if already_archived(fid):
        return FetchResult(fid, candidate.url, "unchanged", len(content)), None

    ext = infer_extension(content_type, candidate.url)
    path = archive_path(root, candidate.source_id, fid, ext)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return (
        FetchResult(fid, candidate.url, "fetched", len(content), storage_path=str(path)),
        path,
    )


def conditional_get(
    url: str,
    *,
    user_agent: str,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout_connect: float = 30,
    timeout_read: float = 120,
    retries: int = 3,
    backoff_base: float = 2,
    backoff_cap: float = 60,
    limiter: DomainRateLimiter | None = None,
    robots: RobotsCache | None = None,
    client: Any = None,
    sleep: Any = time.sleep,
) -> httpx.Response:
    """One polite GET, with the stored validators. §2.3.

    Conditional headers are the difference between a monthly poll costing a
    round trip and costing a download. Most polls should return 304.

    Retries use exponential backoff with jitter. The jitter is not decoration:
    without it, several sources retrying after the same outage synchronise and
    arrive together.
    """
    if robots is not None and not robots.allows(url, user_agent, client):
        raise FetchError(f"robots.txt disallows {url}")

    headers = {"User-Agent": user_agent}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    getter = client or httpx
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        if limiter is not None:
            limiter.acquire(url, sleep=sleep)
        try:
            response: httpx.Response = getter.get(
                url,
                headers=headers,
                timeout=httpx.Timeout(timeout_read, connect=timeout_connect),
                follow_redirects=True,
            )
        except Exception as exc:  # network-level failure
            last_error = exc
        else:
            if response.status_code < 500:
                return response
            last_error = FetchError(f"HTTP {response.status_code} for {url}")

        if attempt < retries:
            delay = min(backoff_cap, backoff_base * (2**attempt))
            sleep(delay * (0.5 + random.random() / 2))

    raise FetchError(f"{url} failed after {retries + 1} attempts: {last_error}")
