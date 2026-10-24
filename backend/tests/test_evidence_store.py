import hashlib

import pytest

from apa.core.settings import Settings
from apa.integrations.evidence_store import (
    EvidenceError,
    LocalEvidenceStore,
    MinioEvidenceStore,
    build_store,
)

JPEG = b"\xff\xd8\xff\xe0" + b"fake jpeg bytes" * 20


@pytest.fixture
def store(tmp_path):
    return LocalEvidenceStore(tmp_path / "ev", max_bytes=10_000)


async def test_round_trip_and_checksum(store):
    f = await store.put(JPEG, "image/jpeg")
    assert f.sha256 == hashlib.sha256(JPEG).hexdigest() and f.size == len(JPEG) and f.mime == "image/jpeg"
    assert f.uri == f"local:{f.sha256[:2]}/{f.sha256}.jpg"
    assert await store.get(f.uri) == JPEG


async def test_content_addressed_so_the_same_bytes_give_the_same_uri(store, tmp_path):
    a, b = await store.put(JPEG, "image/jpeg"), await store.put(JPEG, "image/jpeg")
    assert a == b
    assert len(list((tmp_path / "ev").rglob("*.jpg"))) == 1  # stored once
    c = await store.put(JPEG + b"x", "image/jpeg")
    assert c.uri != a.uri


async def test_no_partial_files_are_left_behind(store, tmp_path):
    await store.put(JPEG, "image/jpeg")
    assert not list((tmp_path / "ev").rglob("*.part"))


@pytest.mark.parametrize(
    ("data", "mime", "why"),
    [(b"", "image/jpeg", "empty"), (b"x" * 10_001, "image/jpeg", "limit"), (JPEG, "text/html", "unsupported")],
)
async def test_unusable_uploads_are_rejected(store, data, mime, why):
    with pytest.raises(EvidenceError, match=why):
        await store.put(data, mime)


async def test_get_refuses_other_schemes_missing_objects_and_path_escapes(store, tmp_path):
    (tmp_path / "secret.txt").write_text("nope")
    for uri in ("s3://b/k", "local:ab/missing.jpg", "local:../secret.txt", "local:../../etc/passwd"):
        with pytest.raises(EvidenceError):
            await store.get(uri)


def test_build_store_picks_the_backend_from_settings(tmp_path):
    assert isinstance(build_store(Settings(evidence_backend="local", evidence_dir=str(tmp_path))), LocalEvidenceStore)
    with pytest.raises(ValueError, match="EVIDENCE_BACKEND"):
        build_store(Settings(evidence_backend="floppy"))


# ---- MinIO store against a fake client (a real MinIO is exercised in the integration tests) -----------------


class FakeMinio:
    def __init__(self):
        self.objects, self.buckets, self.calls = {}, set(), []

    def bucket_exists(self, b):
        return b in self.buckets

    def make_bucket(self, b):
        self.calls.append(("make_bucket", b))
        self.buckets.add(b)

    def put_object(self, bucket, key, data, length, content_type):
        self.objects[(bucket, key)] = (data.read(), content_type)
        assert length == len(self.objects[(bucket, key)][0])

    def get_object(self, bucket, key):
        if (bucket, key) not in self.objects:
            raise RuntimeError("NoSuchKey")
        body = self.objects[(bucket, key)][0]

        class Resp:
            def read(self):
                return body

            def close(self):
                pass

            def release_conn(self):
                pass

        return Resp()


async def test_minio_store_creates_the_bucket_once_and_round_trips():
    c = FakeMinio()
    s = MinioEvidenceStore(c, "evidence", max_bytes=10_000)
    f = await s.put(JPEG, "image/jpeg")
    await s.put(JPEG + b"1", "image/jpeg")
    assert f.uri == f"s3://evidence/{f.sha256[:2]}/{f.sha256}.jpg"
    assert c.calls == [("make_bucket", "evidence")]
    assert c.objects[("evidence", f"{f.sha256[:2]}/{f.sha256}.jpg")][1] == "image/jpeg"
    assert await s.get(f.uri) == JPEG


async def test_minio_store_rejects_foreign_uris_and_wraps_missing_keys():
    s = MinioEvidenceStore(FakeMinio(), "evidence")
    with pytest.raises(EvidenceError, match="not an object"):
        await s.get("s3://other/ab/x.jpg")
    with pytest.raises(EvidenceError, match="cannot read"):
        await s.get("s3://evidence/ab/missing.jpg")
    with pytest.raises(EvidenceError):
        await s.put(b"", "image/jpeg")


def test_s3_backend_builds_a_client_for_garage_with_its_region():
    from apa.integrations.evidence_store import MinioEvidenceStore

    s = Settings(evidence_backend="s3", minio_endpoint="garage.lan:3900", minio_access_key="GK1", minio_secret_key="x",
                 minio_region="garage", minio_bucket_evidence="evidence")
    store = build_store(s)
    assert isinstance(store, MinioEvidenceStore)
    assert store._c._base_url.region == "garage" and store._c._base_url.host == "garage.lan:3900"


# ---- storage chosen in the dashboard ----------------------------------------------------------------------------------------


def test_the_dashboard_storage_choice_overrides_the_env_only_when_set():
    from apa.core.settings import Settings
    from apa.storage_config import StorageCfg, public

    base = Settings(evidence_backend="local", minio_endpoint="old:9000")
    assert StorageCfg().settings(base).minio_endpoint == "old:9000"  # "env" keeps the .env behaviour
    s3 = StorageCfg(backend="s3", endpoint="https://garage:3900/", access_key="k", secret_key="sk-secret-1234", bucket="photos",
                    region="garage", secure=True).settings(base)
    assert (s3.evidence_backend, s3.minio_endpoint, s3.minio_bucket_evidence, s3.minio_secure) == ("s3", "garage:3900", "photos", True)
    assert StorageCfg(backend="local").settings(Settings(evidence_backend="s3")).evidence_backend == "local"
    assert "sk-secret" not in str(public(StorageCfg(backend="s3", secret_key="sk-secret-1234"))) and "1234" in str(public(StorageCfg(secret_key="sk-secret-1234")))
    import pytest

    with pytest.raises(ValueError):
        StorageCfg(endpoint="host/with/path")


def test_a_photo_is_read_from_where_its_address_says_whatever_the_current_backend(tmp_path, monkeypatch):
    import asyncio

    from apa import storage_config
    from apa.core.settings import Settings
    from apa.integrations.evidence_store import DynamicEvidenceStore, EvidenceError

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 600
    base = Settings(evidence_backend="local", evidence_dir=str(tmp_path))

    async def run():
        async def fixed():
            return storage_config.StorageCfg(backend="local")

        monkeypatch.setattr(storage_config, "current", fixed)
        store = DynamicEvidenceStore(base)
        stored = await store.put(png, "image/png")
        assert stored.uri.startswith("local:") and await store.get(stored.uri) == png
        with pytest.raises(EvidenceError):  # an s3:// photo with no S3 store configured is a clear error, not a guess
            await store.get("s3://evidence/ab/abcd.png")

    asyncio.run(run())
