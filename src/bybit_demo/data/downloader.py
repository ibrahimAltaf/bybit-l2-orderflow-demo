"""Async, streaming, hash-recording downloader.

Each retrieval is recorded (URL, local path, byte size, SHA-256, UTC retrieval
time, HTTP metadata) in ``download_manifest.json`` so every file used as
evidence can be independently re-downloaded and re-hashed. Once a URL has a
recorded hash, any later copy -- cached or freshly downloaded -- must match it.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import zipfile
import zlib
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel

from bybit_demo import __version__
from bybit_demo.data.provider_registry import RemoteFile
from bybit_demo.errors import CorruptFileError, DownloadError, IntegrityError

log = logging.getLogger(__name__)

_CHUNK = 1 << 20


class DownloadRecord(BaseModel):
    dataset_id: str
    kind: str
    url: str
    local_path: str
    bytes: int
    sha256: str
    retrieved_at_utc: str
    http_status: int | None
    content_type: str | None
    last_modified: str | None
    from_cache: bool = False


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


class ManifestStore:
    """JSON manifest keyed by URL; preserves original retrieval metadata for cached files."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: dict[str, DownloadRecord] = {}
        if path.exists():
            for item in json.loads(path.read_text(encoding="utf-8")):
                rec = DownloadRecord.model_validate(item)
                self._records[rec.url] = rec

    def get(self, url: str) -> DownloadRecord | None:
        return self._records.get(url)

    def put(self, rec: DownloadRecord) -> None:
        self._records[rec.url] = rec

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = [
            r.model_dump(exclude={"from_cache"})
            for r in sorted(self._records.values(), key=lambda r: r.url)
        ]
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def check_archive_integrity(path: Path, name: str | None = None) -> None:
    """Fully decompress ``path`` (zip CRC check / gzip end-of-stream) or raise CorruptFileError.

    ``name`` selects the format (default ``path.name``; pass the final name when
    checking a ``*.part`` temp file). A file that merely *exists* is never
    trusted: a truncated or damaged archive would otherwise surface later as
    silently missing records. Unknown extensions are an error, not a skip.
    """
    name = name or path.name
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                bad = zf.testzip()
            if bad is not None:
                raise CorruptFileError(f"{name}: CRC mismatch in member {bad!r}")
        elif name.endswith(".gz"):
            with gzip.open(path, "rb") as f:
                while f.read(_CHUNK):
                    pass
        else:
            raise CorruptFileError(f"{name}: unknown archive type; cannot verify integrity")
    except (zipfile.BadZipFile, zlib.error, EOFError, gzip.BadGzipFile) as exc:
        raise CorruptFileError(
            f"{name}: archive is truncated or corrupt ({type(exc).__name__}: {exc})"
        ) from exc


def _cached_record(remote: RemoteFile, prior: DownloadRecord | None) -> DownloadRecord | None:
    """Record for a usable cached copy, or ``None`` if the file must be (re)downloaded."""
    dest = remote.local_path
    if not dest.exists():
        return None
    digest = sha256_file(dest)
    if prior is not None:
        if prior.sha256 == digest:
            return prior.model_copy(update={"from_cache": True})
        log.warning(
            "%s: cached SHA-256 %s differs from manifest %s; re-downloading",
            dest,
            digest[:16],
            prior.sha256[:16],
        )
        return None
    # Present but never recorded (e.g. copied in by hand): accept only if the
    # archive decompresses cleanly; the record says no HTTP retrieval happened.
    try:
        check_archive_integrity(dest)
    except CorruptFileError as exc:
        log.warning("%s; re-downloading", exc)
        return None
    return DownloadRecord(
        dataset_id=remote.dataset_id,
        kind=remote.kind.value,
        url=remote.url,
        local_path=dest.as_posix(),
        bytes=dest.stat().st_size,
        sha256=digest,
        retrieved_at_utc=datetime.fromtimestamp(dest.stat().st_mtime, UTC).isoformat(),
        http_status=None,
        content_type=None,
        last_modified=None,
        from_cache=True,
    )


class HttpStatusError(DownloadError):
    """Non-200 response. 429 and 5xx are retryable; other 4xx (e.g. 404) are not."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500


async def _fetch_once(
    client: httpx.AsyncClient, remote: RemoteFile, tmp: Path
) -> tuple[int, str, httpx.Headers]:
    """Stream one GET into ``tmp``; return (size, sha256, headers). Raises on any defect."""
    h = hashlib.sha256()
    size = 0
    async with client.stream("GET", remote.url) as resp:
        if resp.status_code != 200:
            body = (await resp.aread())[:300].decode("utf-8", "replace")
            raise HttpStatusError(resp.status_code, f"{remote.url}: HTTP {resp.status_code} {body}")
        with tmp.open("wb") as f:
            async for chunk in resp.aiter_bytes(_CHUNK):
                f.write(chunk)
                h.update(chunk)
                size += len(chunk)
        expected = resp.headers.get("content-length")
        # Content-Length counts encoded bytes; compare only for identity-encoded bodies
        # (the verified archives are served without Content-Encoding).
        identity = resp.headers.get("content-encoding", "identity") == "identity"
        if expected is not None and identity and int(expected) != size:
            raise DownloadError(
                f"{remote.url}: received {size} bytes but Content-Length is {expected} "
                "(connection interrupted)"
            )
        return size, h.hexdigest(), resp.headers


async def download_file(
    client: httpx.AsyncClient,
    remote: RemoteFile,
    manifest: ManifestStore,
    *,
    force: bool = False,
    retries: int = 3,
    backoff_s: float = 2.0,
) -> DownloadRecord:
    """Return a verified local copy of ``remote``, downloading only when necessary.

    * A cached copy is reused only if its SHA-256 equals the manifest record
      (or, if unrecorded, only if it decompresses cleanly).
    * A download is written to ``*.part`` and renamed into place (atomic on the
      same filesystem) only after the status, Content-Length and
      archive-integrity checks pass.
    * If the manifest already holds a hash for this URL and the download
      differs, :class:`IntegrityError` is raised and nothing is overwritten:
      the provider's file changed, so recorded evidence no longer describes it.
    * Transport errors, HTTP 429 and 5xx are retried ``retries`` times with
      exponential backoff; other 4xx (e.g. 404 = not published) fail at once.
    """
    dest = remote.local_path
    prior = manifest.get(remote.url)
    if not force and (cached := _cached_record(remote, prior)) is not None:
        manifest.put(cached)
        return cached

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("downloading %s", remote.url)
    attempt = 0
    while True:
        attempt += 1
        try:
            size, digest, headers = await _fetch_once(client, remote, tmp)
            check_archive_integrity(tmp, dest.name)
            break
        except (httpx.TransportError, DownloadError, CorruptFileError) as exc:
            tmp.unlink(missing_ok=True)
            retryable = not isinstance(exc, HttpStatusError) or exc.retryable
            if not retryable or attempt > retries:
                if isinstance(exc, httpx.TransportError):
                    raise DownloadError(
                        f"{remote.url}: network error after {attempt} attempt(s) "
                        f"({type(exc).__name__}: {exc}). No verified cached copy exists, so "
                        "this file is unavailable offline."
                    ) from exc
                raise
            delay = backoff_s * 2 ** (attempt - 1)
            log.warning("%s: %s; retry %d/%d in %.1fs", remote.url, exc, attempt, retries, delay)
            await asyncio.sleep(delay)

    if prior is not None and prior.sha256 != digest:
        tmp.unlink(missing_ok=True)
        raise IntegrityError(
            f"{remote.url}: downloaded SHA-256 {digest} differs from the manifest "
            f"({prior.sha256}, retrieved {prior.retrieved_at_utc}). The provider's file has "
            "changed and the recorded evidence does not describe it; not overwriting."
        )
    tmp.replace(dest)
    rec = DownloadRecord(
        dataset_id=remote.dataset_id,
        kind=remote.kind.value,
        url=remote.url,
        local_path=dest.as_posix(),
        bytes=size,
        sha256=digest,
        # Keep the original retrieval time when re-downloading an identical file.
        retrieved_at_utc=(
            prior.retrieved_at_utc if prior is not None else datetime.now(UTC).isoformat()
        ),
        http_status=200,
        content_type=headers.get("content-type"),
        last_modified=headers.get("last-modified"),
        from_cache=False,
    )
    manifest.put(rec)
    return rec


async def download_all(
    files: list[RemoteFile],
    manifest: ManifestStore,
    *,
    concurrency: int = 2,
    timeout_s: float = 600.0,
    transport: httpx.AsyncBaseTransport | None = None,
    backoff_s: float = 2.0,
) -> list[DownloadRecord]:
    """Download several files with bounded concurrency; saves the manifest at the end."""
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        timeout=timeout_s,
        follow_redirects=True,
        transport=transport,
        headers={"User-Agent": f"bybit-l2-feasibility/{__version__}"},
    ) as client:

        async def one(rf: RemoteFile) -> DownloadRecord:
            async with sem:
                return await download_file(client, rf, manifest, backoff_s=backoff_s)

        try:
            return list(await asyncio.gather(*(one(f) for f in files)))
        finally:
            manifest.save()
