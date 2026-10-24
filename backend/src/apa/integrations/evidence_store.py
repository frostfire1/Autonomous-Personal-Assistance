"""Where receipt photos live. Content-addressed: the object name is the SHA-256 of the bytes, so the
same image always maps to the same URI and a duplicate upload is detectable for free.

`local` is a directory (dev, tests); `s3` (alias `minio`) is the production store, Garage here (blueprint 5C).
Bytes never travel through Redis or Postgres: events carry only the URI and the checksum.
"""

import asyncio
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from apa.core.settings import Settings, get_settings

_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "application/pdf": "pdf"}


class EvidenceError(Exception):
    """Unusable upload (empty, too large, unsupported type) or a missing object."""


@dataclass(frozen=True)
class StoredFile:
    uri: str  # 'local:ab/ab12...jpg' or 's3://bucket/ab/ab12...jpg'
    sha256: str
    size: int
    mime: str


class EvidenceStore(Protocol):
    async def put(self, data: bytes, mime: str) -> StoredFile: ...

    async def get(self, uri: str) -> bytes: ...


def _validate(data: bytes, mime: str, max_bytes: int) -> str:
    if mime not in _EXT:
        raise EvidenceError(f"unsupported media type {mime!r}")
    if not data:
        raise EvidenceError("empty file")
    if len(data) > max_bytes:
        raise EvidenceError(f"file is {len(data)} bytes, the limit is {max_bytes}")
    return hashlib.sha256(data).hexdigest()


def _key(sha: str, mime: str) -> str:
    return f"{sha[:2]}/{sha}.{_EXT[mime]}"


class LocalEvidenceStore:
    def __init__(self, root: str | Path, max_bytes: int = 10 * 1024 * 1024):
        self._root, self._max = Path(root), max_bytes

    async def put(self, data: bytes, mime: str) -> StoredFile:
        sha = _validate(data, mime, self._max)
        key = _key(sha, mime)
        path = self._root / key

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():  # same bytes, same name: idempotent
                tmp = path.with_suffix(path.suffix + ".part")
                tmp.write_bytes(data)
                tmp.replace(path)  # atomic: a reader never sees a half-written image

        await asyncio.to_thread(write)
        return StoredFile(f"local:{key}", sha, len(data), mime)

    async def get(self, uri: str) -> bytes:
        if not uri.startswith("local:"):
            raise EvidenceError(f"not a local uri: {uri!r}")
        path = (self._root / uri.removeprefix("local:")).resolve()
        if self._root.resolve() not in path.parents:  # no ../ escapes
            raise EvidenceError("uri escapes the evidence directory")
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as e:
            raise EvidenceError(f"missing object {uri!r}") from e


class MinioEvidenceStore:
    """S3-compatible store. The minio client is synchronous, so calls run in a worker thread."""

    def __init__(self, client, bucket: str, max_bytes: int = 10 * 1024 * 1024):
        self._c, self._bucket, self._max = client, bucket, max_bytes
        self._ready = False

    def _ensure_bucket(self) -> None:
        if not self._ready:
            if not self._c.bucket_exists(self._bucket):
                self._c.make_bucket(self._bucket)
            self._ready = True

    async def put(self, data: bytes, mime: str) -> StoredFile:
        sha = _validate(data, mime, self._max)
        key = _key(sha, mime)

        def upload() -> None:
            self._ensure_bucket()
            self._c.put_object(self._bucket, key, io.BytesIO(data), len(data), content_type=mime)

        await asyncio.to_thread(upload)
        return StoredFile(f"s3://{self._bucket}/{key}", sha, len(data), mime)

    async def get(self, uri: str) -> bytes:
        prefix = f"s3://{self._bucket}/"
        if not uri.startswith(prefix):
            raise EvidenceError(f"not an object of bucket {self._bucket!r}: {uri!r}")

        def download() -> bytes:
            resp = self._c.get_object(self._bucket, uri.removeprefix(prefix))
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()

        try:
            return await asyncio.to_thread(download)
        except Exception as e:  # minio raises S3Error for a missing key
            raise EvidenceError(f"cannot read {uri!r}: {e}") from e


def build_store(s: Settings | None = None) -> EvidenceStore:
    s = s or get_settings()
    if s.evidence_backend in ("s3", "minio"):  # Garage, MinIO or any S3-compatible store
        from minio import Minio

        client = Minio(
            s.minio_endpoint, s.minio_access_key, s.minio_secret_key, secure=s.minio_secure,
            region=s.minio_region or None,  # Garage's default region is "garage"; set it to skip the lookup
        )
        return MinioEvidenceStore(client, s.minio_bucket_evidence, s.max_image_bytes)
    if s.evidence_backend == "local":
        return LocalEvidenceStore(s.evidence_dir, s.max_image_bytes)
    raise ValueError(f"unknown EVIDENCE_BACKEND {s.evidence_backend!r} (use 'local' or 's3')")


class DynamicEvidenceStore:
    """The store the owner chose in the dashboard (this server's disk or an S3 store), re-read within seconds
    of a change, so switching needs no restart. Reads follow the object's own address: an old `local:`
    photo is still read from disk and an `s3://` photo from the S3 store, whichever backend is new."""

    def __init__(self, base: Settings | None = None):
        self._base = base
        self._built: dict[tuple, EvidenceStore] = {}

    async def _for(self, *, s3: bool | None = None) -> EvidenceStore:
        from apa import storage_config

        cfg = await storage_config.current()
        eff = cfg.settings(self._base)
        if s3 is True and eff.evidence_backend not in ("s3", "minio"):
            raise EvidenceError("no S3 store is configured to read this photo from")
        if s3 is False:
            eff = eff.model_copy(update={"evidence_backend": "local"})
        key = (eff.evidence_backend, eff.minio_endpoint, eff.minio_access_key, eff.minio_secret_key,
               eff.minio_bucket_evidence, eff.minio_region, eff.minio_secure)
        if key not in self._built:
            self._built = {key: build_store(eff)}  # one live store at a time: a replaced one is dropped
        return self._built[key]

    async def put(self, data: bytes, mime: str) -> StoredFile:
        return await (await self._for()).put(data, mime)

    async def get(self, uri: str) -> bytes:
        return await (await self._for(s3=not uri.startswith("local:"))).get(uri)
