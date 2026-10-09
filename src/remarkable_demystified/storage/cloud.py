"""The reMarkable cloud ("sync 1.5" / schema v3-v4), reimplemented from rmapi.

How the cloud stores a library
------------------------------
Everything is a content-addressed blob (``GET/PUT /sync/v3/files/<hash>``):

* every file of a document (``<id>.metadata``, ``<id>/<page>.rm``, the PDF...)
  is a blob whose hash is the SHA-256 of its bytes;
* each document has an *index* blob listing its files,
  one ``<hash>:0:<file name>:0:<size>`` line each (schema ``3``); its hash is
  the SHA-256 of the concatenated raw file hashes, sorted by file name;
* the *root index* lists every document, ``<doc hash>:0:<doc id>:<files>:<size>``
  sorted by id, with a ``4`` / ``0:.:<count>:<total size>`` header; its hash is
  the SHA-256 of the whole body;
* ``/sync/v4/root`` holds the current root hash plus a *generation* number.
  Updating it with a stale generation fails with HTTP 412, which is how
  concurrent edits from the tablet are detected; we then re-read and retry.

Authentication is a two-step token exchange: an 8-letter one-time code from
https://my.remarkable.com/device/browser/connect is traded once for a long-lived
*device token*, which in turn mints short-lived *user tokens* (JWTs).

The protocol is undocumented. These details match rmapi
(https://github.com/ddvk/rmapi, August 2026), which has to keep up with server-side
changes; if reMarkable changes the protocol, writes fail with an HTTP error rather
than corrupting anything, because the root is only swapped after every blob is
uploaded.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import struct
import threading
import time
import uuid
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .. import xochitl
from ..errors import AuthError, Conflict, ConnectionFailed, NotFound, RemarkableError
from .base import DocChange, DocumentRecord, DocumentStorage

log = logging.getLogger(__name__)

AUTH_HOST = "https://webapp-prod.cloud.remarkable.engineering"
SYNC_HOST = "https://internal.cloud.remarkable.com"
CONNECT_URL = "https://my.remarkable.com/device/browser/connect"
USER_AGENT = "remarkable-demystified/0.1"
CACHE_VERSION = 1


# ---------------------------------------------------------------------------
# CRC32C (Castagnoli), required by the blob store's x-goog-hash header
# ---------------------------------------------------------------------------


def _make_crc_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0x82F63B78 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_CRC_TABLE = _make_crc_table()


def crc32c(data: bytes) -> int:
    for module in ("google_crc32c", "crc32c"):
        try:
            mod = __import__(module)
        except ImportError:
            continue
        if module == "google_crc32c":
            return int.from_bytes(mod.Checksum(data).digest(), "big")  # type: ignore[attr-defined]
        return mod.crc32c(data)  # type: ignore[attr-defined]
    crc = 0xFFFFFFFF
    table = _CRC_TABLE
    for b in data:
        crc = table[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def goog_hash(data: bytes) -> str:
    return "crc32c=" + base64.b64encode(struct.pack(">I", crc32c(data))).decode()


# ---------------------------------------------------------------------------
# Index files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexEntry:
    hash: str
    type: str
    name: str  # doc id (root index) or file name (document index)
    count: int
    size: int


def parse_index(data: bytes) -> tuple[str, list[IndexEntry]]:
    """Parse a root or document index (schema 3 or 4)."""
    lines = data.decode("utf-8").split("\n")
    if not lines or lines[0] not in ("3", "4"):
        raise RemarkableError(f"unsupported cloud index schema {lines[0][:10]!r}")
    schema = lines[0]
    body = lines[2:] if schema == "4" else lines[1:]
    entries = []
    for line in body:
        if not line:
            continue
        parts = line.split(":")
        if len(parts) != 5:
            raise RemarkableError(f"malformed cloud index line {line!r}")
        entries.append(IndexEntry(parts[0], parts[1], parts[2], int(parts[3]), int(parts[4])))
    return schema, entries


def doc_index(files: Iterable[IndexEntry]) -> tuple[str, bytes]:
    """Serialize a document index (schema 3) and compute its hash, as rmapi does."""
    ordered = sorted(files, key=lambda e: e.name)
    body = "3\n" + "".join(f"{e.hash}:0:{e.name}:0:{e.size}\n" for e in ordered)
    digest = hashlib.sha256(b"".join(bytes.fromhex(e.hash) for e in ordered)).hexdigest()
    return digest, body.encode()


def root_index(docs: Iterable[IndexEntry]) -> tuple[str, bytes]:
    """Serialize the root index (schema 4, sorted by id). Its hash is SHA-256 of the body."""
    ordered = sorted(docs, key=lambda e: e.name)
    total = sum(e.size for e in ordered)
    body = f"4\n0:.:{len(ordered)}:{total}\n" + "".join(
        f"{e.hash}:0:{e.name}:{e.count}:{e.size}\n" for e in ordered
    )
    data = body.encode()
    return hashlib.sha256(data).hexdigest(), data


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def _jwt_expiry(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, KeyError, ValueError, TypeError):
        return None


class CloudAuth:
    """Device/user tokens, persisted in a JSON file (mode 0600)."""

    def __init__(self, token_file: str | os.PathLike[str], *, auth_host: str = AUTH_HOST,
                 client: httpx.Client | None = None) -> None:
        self.token_file = Path(token_file).expanduser()
        self.auth_host = auth_host.rstrip("/")
        self.client = client or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
        self._lock = threading.Lock()
        try:
            self._tokens: dict[str, str] = json.loads(self.token_file.read_text())
        except FileNotFoundError:
            self._tokens = {}
        except ValueError as exc:
            raise AuthError(f"{self.token_file} is corrupt; register again") from exc

    @property
    def registered(self) -> bool:
        return bool(self._tokens.get("device_token"))

    def _save(self) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # never world-readable
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(self._tokens, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.token_file)

    def register(self, code: str, *, description: str = "desktop-linux") -> None:
        """Trade a one-time code from my.remarkable.com for a device token."""
        code = code.strip()
        if len(code) != 8:
            raise AuthError(f"one-time codes are 8 characters; get one at {CONNECT_URL}")
        device_id = str(uuid.uuid4())
        try:
            resp = self.client.post(
                f"{self.auth_host}/token/json/2/device/new",
                json={"code": code, "deviceDesc": description, "deviceID": device_id},
                headers={"Authorization": "Bearer"},
            )
        except httpx.HTTPError as exc:
            raise ConnectionFailed(f"cannot reach the reMarkable cloud: {exc}") from exc
        if resp.status_code != 200 or not resp.text.strip():
            raise AuthError(
                f"code rejected (HTTP {resp.status_code}); codes expire after a few minutes, "
                f"get a fresh one at {CONNECT_URL}"
            )
        self._tokens = {"device_token": resp.text.strip(), "device_id": device_id}
        self._save()

    def user_token(self, *, force: bool = False) -> str:
        with self._lock:
            token = self._tokens.get("user_token")
            exp = _jwt_expiry(token) if token else None
            if token and not force and (exp is None or exp - time.time() > 300):
                return token
            device = self._tokens.get("device_token")
            if not device:
                raise AuthError(f"not registered with the reMarkable cloud; run `rmd cloud-login <code>` "
                                f"with a code from {CONNECT_URL}")
            try:
                resp = self.client.post(
                    f"{self.auth_host}/token/json/2/user/new",
                    headers={"Authorization": f"Bearer {device}"},
                )
            except httpx.HTTPError as exc:
                raise ConnectionFailed(f"cannot reach the reMarkable cloud: {exc}") from exc
            if resp.status_code in (401, 403):
                raise AuthError("the cloud no longer accepts this device token; register again")
            if resp.status_code != 200 or not resp.text.strip():
                raise AuthError(f"could not refresh the cloud session (HTTP {resp.status_code})")
            self._tokens["user_token"] = resp.text.strip()
            self._save()
            return self._tokens["user_token"]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class _WrongGeneration(Exception):
    pass


@dataclass
class _Doc:
    hash: str
    size: int
    files: dict[str, IndexEntry] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    content: dict[str, Any] = field(default_factory=dict)


class CloudStorage(DocumentStorage):
    name = "cloud"
    cache_ttl = 15.0

    def __init__(
        self,
        auth: CloudAuth,
        *,
        sync_host: str = SYNC_HOST,
        cache_dir: str | os.PathLike[str] | None = None,
        client: httpx.Client | None = None,
        concurrency: int = 8,
        notify: bool = True,
    ) -> None:
        super().__init__()
        self.auth = auth
        self.sync_host = sync_host.rstrip("/")
        self.client = client or httpx.Client(timeout=120, headers={"User-Agent": USER_AGENT})
        self.concurrency = max(1, concurrency)
        #: Ask the cloud to push the change to the tablet right away.
        self.notify = notify
        self.cache_file = Path(cache_dir).expanduser() / "cloud-tree.json" if cache_dir else None
        self._lock = threading.RLock()
        self.root_hash = ""
        self.generation = 0
        self.docs: dict[str, _Doc] = {}
        self._load_cache()

    # -- HTTP --------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        for attempt in range(2):
            headers["Authorization"] = f"Bearer {self.auth.user_token(force=attempt > 0)}"
            try:
                resp = self.client.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise ConnectionFailed(f"cloud request failed: {exc}") from exc
            if resp.status_code != 401:
                break
        if resp.status_code in (200, 201, 202):
            return resp
        if resp.status_code == 401:
            raise AuthError("the reMarkable cloud rejected our session")
        if resp.status_code == 404:
            raise NotFound(f"cloud object not found: {url.rsplit('/', 1)[-1]}")
        if resp.status_code == 412:
            raise _WrongGeneration()
        raise RemarkableError(f"cloud returned HTTP {resp.status_code} for {method} {url}: {resp.text[:200]}")

    def get_root(self) -> tuple[str, int]:
        try:
            data = self._request("GET", f"{self.sync_host}/sync/v4/root").json()
        except NotFound:
            return "", 0  # brand-new account
        return str(data.get("hash") or ""), int(data.get("generation") or 0)

    def get_blob(self, blob_hash: str, name: str) -> bytes:
        return self._request(
            "GET", f"{self.sync_host}/sync/v3/files/{blob_hash}", headers={"rm-filename": name}
        ).content

    def put_blob(self, blob_hash: str, name: str, data: bytes) -> None:
        content_type = "text/plain; charset=UTF-8" if name == "root.docSchema" else "application/octet-stream"
        self._request(
            "PUT",
            f"{self.sync_host}/sync/v3/files/{blob_hash}",
            content=data,
            headers={"rm-filename": name, "x-goog-hash": goog_hash(data), "content-type": content_type},
        )

    def put_root(self, root_hash: str, generation: int) -> int:
        resp = self._request(
            "PUT",
            f"{self.sync_host}/sync/v3/root",
            json={"broadcast": self.notify, "generation": generation, "hash": root_hash},
            headers={"rm-filename": "roothash"},
        ).json()
        if resp.get("hash") != root_hash:
            raise RemarkableError("cloud accepted a different root hash than the one sent")
        return int(resp["generation"])

    def _parallel(self, fn, items: list) -> list:
        if len(items) <= 1:
            return [fn(i) for i in items]
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            return list(pool.map(fn, items))

    # -- mirroring ---------------------------------------------------------

    def _load_cache(self) -> None:
        if not self.cache_file or not self.cache_file.exists():
            return
        try:
            raw = json.loads(self.cache_file.read_text())
            if raw.get("version") != CACHE_VERSION:
                return
            self.root_hash, self.generation = raw["hash"], raw["generation"]
            self.docs = {
                doc_id: _Doc(d["hash"], d["size"],
                             {f[2]: IndexEntry(*f) for f in d["files"]}, d["metadata"], d["content"])
                for doc_id, d in raw["docs"].items()
            }
        except (ValueError, KeyError, TypeError):
            log.warning("ignoring corrupt cloud cache %s", self.cache_file)
            self.root_hash, self.generation, self.docs = "", 0, {}

    def _save_cache(self) -> None:
        if not self.cache_file:
            return
        raw = {
            "version": CACHE_VERSION,
            "hash": self.root_hash,
            "generation": self.generation,
            "docs": {
                doc_id: {
                    "hash": d.hash, "size": d.size, "metadata": d.metadata, "content": d.content,
                    "files": [[e.hash, e.type, e.name, e.count, e.size] for e in d.files.values()],
                }
                for doc_id, d in self.docs.items()
            },
        }
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw))
        os.replace(tmp, self.cache_file)

    def _fetch_doc(self, entry: IndexEntry, old: _Doc | None) -> _Doc:
        _schema, files = parse_index(self.get_blob(entry.hash, f"{entry.name}.docSchema"))
        doc = _Doc(entry.hash, entry.size, {f.name: f for f in files})
        meta_name, content_name = xochitl.metadata_name(entry.name), xochitl.content_name(entry.name)
        for fname, attr in ((meta_name, "metadata"), (content_name, "content")):
            f = doc.files.get(fname)
            if f is None:
                continue
            prev = old.files.get(fname) if old else None
            if prev is not None and prev.hash == f.hash:
                setattr(doc, attr, getattr(old, attr))
            else:
                setattr(doc, attr, xochitl.loads(self.get_blob(f.hash, fname)))
        return doc

    def sync(self) -> None:
        """Bring the local mirror up to date with the cloud's current root."""
        with self._lock:
            root_hash, generation = self.get_root()
            if root_hash == self.root_hash and root_hash:
                self.generation = generation
                return
            docs: dict[str, _Doc] = {}
            if root_hash:
                _schema, entries = parse_index(self.get_blob(root_hash, "root.docSchema"))
                stale = [e for e in entries if e.name not in self.docs or self.docs[e.name].hash != e.hash]
                fresh = self._parallel(lambda e: (e.name, self._fetch_doc(e, self.docs.get(e.name))), stale)
                for e in entries:
                    if e.name in self.docs and self.docs[e.name].hash == e.hash:
                        docs[e.name] = self.docs[e.name]
                docs.update(dict(fresh))
            self.root_hash, self.generation, self.docs = root_hash, generation, docs
            self._save_cache()

    def _load_documents(self) -> dict[str, DocumentRecord]:
        self.sync()
        return {
            doc_id: DocumentRecord(doc_id, d.metadata, d.content)
            for doc_id, d in self.docs.items()
            if d.metadata
        }

    def _doc(self, doc_id: str) -> _Doc:
        self.documents()
        doc = self.docs.get(doc_id)
        if doc is None:
            raise NotFound(f"no document {doc_id!r} in the cloud")
        return doc

    def list_files(self, doc_id: str) -> list[str]:
        return sorted(self._doc(doc_id).files)

    def read_file(self, doc_id: str, name: str) -> bytes:
        entry = self._doc(doc_id).files.get(name)
        if entry is None:
            raise NotFound(f"{name} does not exist in the cloud")
        return self.get_blob(entry.hash, name)

    def read_files(self, doc_id: str, names: Iterable[str]) -> dict[str, bytes]:
        names = list(names)
        return dict(zip(names, self._parallel(lambda n: self.read_file(doc_id, n), names), strict=True))

    # -- writing -----------------------------------------------------------

    def _apply(self, changes: list[DocChange]) -> None:
        with self._lock:
            uploaded: set[str] = set()
            for _attempt in range(5):
                self.sync()
                docs = dict(self.docs)
                blobs: dict[str, tuple[str, bytes]] = {}
                for change in changes:
                    if change.delete:
                        docs.pop(change.doc_id, None)
                        continue
                    old = docs.get(change.doc_id)
                    files = dict(old.files) if old else {}
                    for name in change.remove:
                        files.pop(name, None)
                    for name, data in change.write.items():
                        if not xochitl.is_safe_file_name(change.doc_id, name):
                            raise RemarkableError(f"{name!r} is not a valid file of {change.doc_id}")
                        h = hashlib.sha256(data).hexdigest()
                        files[name] = IndexEntry(h, "0", name, 0, len(data))
                        if h not in uploaded:
                            blobs[h] = (name, data)
                    if not files:
                        docs.pop(change.doc_id, None)
                        continue
                    doc_hash, index_body = doc_index(files.values())
                    if doc_hash not in uploaded:
                        blobs[doc_hash] = (f"{change.doc_id}.docSchema", index_body)
                    meta = files.get(xochitl.metadata_name(change.doc_id))
                    new = _Doc(doc_hash, sum(f.size for f in files.values()), files,
                               old.metadata if old else {}, old.content if old else {})
                    if xochitl.metadata_name(change.doc_id) in change.write:
                        new.metadata = xochitl.loads(change.write[xochitl.metadata_name(change.doc_id)])
                    if xochitl.content_name(change.doc_id) in change.write:
                        new.content = xochitl.loads(change.write[xochitl.content_name(change.doc_id)])
                    if meta is None:
                        raise RemarkableError(f"document {change.doc_id} would have no .metadata")
                    docs[change.doc_id] = new

                root_hash, root_body = root_index(
                    IndexEntry(d.hash, "0", doc_id, len(d.files), d.size) for doc_id, d in docs.items()
                )
                # Upload every blob before touching the root, so a failure leaves the cloud
                # untouched; files first, then the document indexes that reference them.
                for is_index in (False, True):
                    batch = [kv for kv in blobs.items() if kv[1][0].endswith(".docSchema") == is_index]
                    self._parallel(lambda kv: self.put_blob(kv[0], kv[1][0], kv[1][1]), batch)
                    uploaded.update(h for h, _ in batch)
                self.put_blob(root_hash, "root.docSchema", root_body)
                try:
                    self.generation = self.put_root(root_hash, self.generation)
                except _WrongGeneration:
                    log.info("cloud changed while writing; re-reading and retrying")
                    continue
                self.root_hash, self.docs = root_hash, docs
                self._save_cache()
                return
            raise Conflict("the cloud kept changing while we tried to write; try again")

    def device_info(self) -> dict[str, Any]:
        self.documents()
        return {
            "backend": self.name,
            "sync_host": self.sync_host,
            "root_hash": self.root_hash,
            "generation": self.generation,
        }

    def close(self) -> None:
        self.client.close()
