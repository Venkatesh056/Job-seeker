"""Where files go — resumes, tailored renders, and the screenshots that prove a submission.

Four places built their own path out of `settings.storage_dir` before this module existed,
which was fine while the answer was always "a folder on this laptop". It stops being fine
the moment there is a second one: a screenshot is the *evidence* that an application was
submitted, and evidence on a container's local disk is evidence that disappears on the next
deploy.

So this is one API with two backends, and the local one stays the default because for the
shipped configuration — one person, one machine — it is the right answer and involves no
network.

**A key is not a path.** Callers name a logical object (`renders/<id>.pdf`); the backend
decides where the bytes live. That is what lets the same code write to a folder here and a
bucket there, and it is also what keeps a caller from building a path out of user input and
escaping the directory — `_safe_key` refuses anything with `..` or a leading slash rather
than normalising it, because normalising a hostile path quietly produces a valid one.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from ..config import get_settings
from ..logging import get_logger

log = get_logger("storage.objects")

# Deliberately narrow. Anything outside this is refused rather than sanitised: a key is
# built by this codebase, so a key that fails here is a bug to fix, not input to clean.
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,240}$")


class UnsafeKey(ValueError):
    """A key that would escape the store, or that no backend could represent."""


def _safe_key(key: str) -> str:
    candidate = (key or "").strip()
    if not _KEY.match(candidate) or ".." in candidate or candidate.endswith("/"):
        raise UnsafeKey(
            f"{key!r} is not a usable object key — letters, digits, dot, dash, underscore "
            "and forward slash only, no parent references"
        )
    return candidate


@dataclass(frozen=True)
class StoredObject:
    key: str
    size_bytes: int
    modified_at: datetime
    # Where the bytes are on this machine, when they are on this machine at all. `None`
    # for a remote backend, and callers have to cope with that rather than assuming a path
    # exists — which is the whole reason the field is optional instead of a lie.
    local_path: Path | None = None


class Backend(Protocol):
    async def put(self, key: str, data: bytes, content_type: str = "") -> StoredObject: ...
    async def get(self, key: str) -> bytes: ...
    async def exists(self, key: str) -> bool: ...
    async def delete(self, key: str) -> bool: ...
    async def list(self, prefix: str = "") -> list[StoredObject]: ...
    def describe(self) -> str: ...


class LocalBackend:
    """A directory. The default, and correct for one machine."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self._root / _safe_key(key)).resolve()
        # Belt and braces. `_safe_key` already refuses `..`, but a symlink inside the store
        # could still point outside it, and that check can only be made after resolving.
        if not str(path).startswith(str(self._root.resolve())):
            raise UnsafeKey(f"{key!r} resolves outside the object store")
        return path

    async def put(self, key: str, data: bytes, content_type: str = "") -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside, then rename. A reader that opens the file while it is being written
        # sees either the old bytes or the new ones, never half of a PDF — and on a crash
        # what is left behind is a temp file rather than a truncated artifact.
        temporary = path.with_suffix(path.suffix + ".partial")
        temporary.write_bytes(data)
        temporary.replace(path)
        return StoredObject(
            key=key,
            size_bytes=len(data),
            modified_at=datetime.now(UTC),
            local_path=path,
        )

    async def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    async def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    async def delete(self, key: str) -> bool:
        path = self._path(key)
        if not path.is_file():
            return False
        path.unlink()
        return True

    async def list(self, prefix: str = "") -> list[StoredObject]:
        base = self._root / prefix if prefix else self._root
        if not base.exists():
            return []
        out: list[StoredObject] = []
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix == ".partial":
                continue
            stat = path.stat()
            out.append(
                StoredObject(
                    key=path.relative_to(self._root).as_posix(),
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                    local_path=path,
                )
            )
        return out

    def describe(self) -> str:
        return f"local ({self._root})"

    def free_bytes(self) -> int:
        return shutil.disk_usage(self._root).free


class SupabaseBackend:
    """Supabase Storage over its REST API.

    REST rather than the S3-compatible endpoint because S3 means SigV4, and SigV4 means
    either `boto3` — a large dependency for four calls — or a hand-rolled signer, which is
    exactly the kind of thing that is subtly wrong for a year.

    **Unexercised.** There is no Supabase project attached to this deployment, so this code
    has never made a real request. It is written and typed and wired; it is not verified,
    and it is labelled that way in `describe()` so nothing downstream can report it as
    working on the strength of it existing.
    """

    def __init__(self, url: str, service_key: str, bucket: str = "careeragent") -> None:
        self._base = url.rstrip("/") + "/storage/v1/object"
        self._key = service_key
        self._bucket = bucket

    def _headers(self, content_type: str = "") -> dict[str, str]:
        headers = {"authorization": f"Bearer {self._key}"}
        if content_type:
            headers["content-type"] = content_type
        return headers

    async def _client(self) -> Any:
        import httpx

        return httpx.AsyncClient(timeout=get_settings().http_timeout_seconds)

    async def put(self, key: str, data: bytes, content_type: str = "") -> StoredObject:
        safe = _safe_key(key)
        async with await self._client() as client:
            response = await client.post(
                f"{self._base}/{self._bucket}/{safe}",
                content=data,
                headers={
                    **self._headers(content_type or "application/octet-stream"),
                    # Without this an upload to an existing key is a 409 rather than a
                    # replacement, and a re-rendered resume would fail to save.
                    "x-upsert": "true",
                },
            )
            response.raise_for_status()
        return StoredObject(key=key, size_bytes=len(data), modified_at=datetime.now(UTC))

    async def get(self, key: str) -> bytes:
        async with await self._client() as client:
            response = await client.get(
                f"{self._base}/{self._bucket}/{_safe_key(key)}", headers=self._headers()
            )
            response.raise_for_status()
            return response.content

    async def exists(self, key: str) -> bool:
        async with await self._client() as client:
            response = await client.head(
                f"{self._base}/{self._bucket}/{_safe_key(key)}", headers=self._headers()
            )
            return response.status_code < 400

    async def delete(self, key: str) -> bool:
        async with await self._client() as client:
            response = await client.delete(
                f"{self._base}/{self._bucket}/{_safe_key(key)}", headers=self._headers()
            )
            return response.status_code < 400

    async def list(self, prefix: str = "") -> list[StoredObject]:
        async with await self._client() as client:
            response = await client.post(
                f"{self._base.replace('/object', '/object/list')}/{self._bucket}",
                json={"prefix": prefix, "limit": 1000},
                headers=self._headers("application/json"),
            )
            response.raise_for_status()
            rows = response.json()
        out: list[StoredObject] = []
        for row in rows if isinstance(rows, list) else []:
            metadata = row.get("metadata") or {}
            out.append(
                StoredObject(
                    key=f"{prefix}/{row.get('name')}" if prefix else str(row.get("name")),
                    size_bytes=int(metadata.get("size") or 0),
                    modified_at=datetime.now(UTC),
                )
            )
        return out

    def describe(self) -> str:
        return f"supabase (bucket {self._bucket}) — configured but never exercised"


# ─── The store ──────────────────────────────────────────────────────────────


class ObjectStore:
    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    async def put(self, key: str, data: bytes, content_type: str = "") -> StoredObject:
        stored = await self._backend.put(key, data, content_type)
        log.debug("object_stored", key=key, bytes=len(data))
        return stored

    async def put_file(self, key: str, path: Path, content_type: str = "") -> StoredObject:
        return await self.put(key, Path(path).read_bytes(), content_type)

    async def get(self, key: str) -> bytes:
        return await self._backend.get(key)

    async def exists(self, key: str) -> bool:
        return await self._backend.exists(key)

    async def delete(self, key: str) -> bool:
        return await self._backend.delete(key)

    async def list(self, prefix: str = "") -> list[StoredObject]:
        return await self._backend.list(prefix)

    async def prune(self, prefix: str, *, older_than_days: int, keep_at_least: int = 0) -> int:
        """Delete old objects under `prefix`. Returns how many went.

        For screenshots, which accumulate: every application attempt leaves several, and
        nothing has ever removed one. `keep_at_least` keeps the newest N regardless of age,
        because "delete everything older than 30 days" on a system nobody has used for 31
        days deletes the only evidence it has.
        """
        objects = sorted(await self.list(prefix), key=lambda o: o.modified_at, reverse=True)
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        removed = 0
        for stored in objects[keep_at_least:]:
            if stored.modified_at < cutoff and await self.delete(stored.key):
                removed += 1
        if removed:
            log.info("objects_pruned", prefix=prefix, removed=removed)
        return removed

    async def usage(self, prefix: str = "") -> dict[str, object]:
        objects = await self.list(prefix)
        return {
            "objects": len(objects),
            "bytes": sum(o.size_bytes for o in objects),
            "backend": self._backend.describe(),
        }

    async def materialise(self, key: str) -> Path | None:
        """A real local path for an object, whatever backend holds it.

        Needed because some consumers cannot take bytes: an email attachment wants a file,
        and `FileResponse` wants a path. A local backend hands back where the bytes already
        are; a remote one writes them to a temp file first.

        Accepts a legacy absolute path as well as a key. Renders written before the store
        existed recorded a full filesystem path in the same column, and refusing to read
        them would break every existing render to tidy a naming convention.
        """
        if not key:
            return None

        # Legacy: an absolute path recorded before keys existed.
        candidate = Path(key)
        if candidate.is_absolute():
            return candidate if candidate.is_file() else None

        backend = self._backend
        if isinstance(backend, LocalBackend):
            path = backend._path(key)
            return path if path.is_file() else None

        try:
            data = await backend.get(key)
        except Exception:
            return None

        import tempfile

        suffix = Path(key).suffix or ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(data)
        return Path(handle.name)

    def describe(self) -> str:
        return self._backend.describe()


_store: ObjectStore | None = None


def get_store() -> ObjectStore:
    """The process-wide store. Local unless Supabase is fully configured."""
    global _store
    if _store is not None:
        return _store

    settings = get_settings()
    url = (getattr(settings, "supabase_url", "") or "").strip()
    key = (getattr(settings, "supabase_service_key", "") or "").strip()
    # Both, or neither. A URL with no key produces 401s at the moment somebody tries to
    # save a resume, which is the worst time to discover a half-finished configuration.
    if url and key:
        _store = ObjectStore(SupabaseBackend(url, key))
        log.info("object_store_backend", backend="supabase")
    else:
        _store = ObjectStore(LocalBackend(Path(settings.storage_dir)))
    return _store


def reset_store() -> None:
    """For tests, and for a settings change that should take effect without a restart."""
    global _store
    _store = None
