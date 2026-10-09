"""An in-memory imitation of the reMarkable cloud sync API, for tests.

It is strict on purpose: every invariant the real service is known to check
(content hashes, sorted indexes, root schema v4, CRC32C headers, generation
numbers) raises a 400/412 here, so the client code can't silently drift.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import time
from pathlib import Path

import httpx

from remarkable_demystified.storage.cloud import crc32c, parse_index

DEVICE_CODE = "abcdefgh"
DEVICE_TOKEN = "device-token-123"


def fake_jwt(exp: float) -> str:
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'none'})}.{enc({'exp': exp, 'sub': 'user'})}.sig"


class FakeCloud:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.root_hash = ""
        self.generation = 0
        self.user_tokens: set[str] = set()
        self.requests: list[tuple[str, str]] = []
        self.root_puts = 0
        self.fail_next_root_puts = 0  # simulate the tablet syncing concurrently
        self.token_lifetime = 3600.0
        self.transport = httpx.MockTransport(self.handle)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport)

    # -- seeding (written the way the tablet writes: schema 4 doc indexes) ---

    def seed_from_dir(self, root: Path) -> None:
        docs = {}
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.name.endswith(".local"):
                rel = str(path.relative_to(root))
                doc_id = rel.split("/")[0].split(".")[0]
                docs.setdefault(doc_id, []).append((rel, path.read_bytes()))
        entries = []
        for doc_id, files in sorted(docs.items()):
            lines = []
            for name, data in sorted(files):
                h = hashlib.sha256(data).hexdigest()
                self.blobs[h] = data
                lines.append(f"{h}:0:{name}:0:{len(data)}")
            size = sum(len(d) for _, d in files)
            body = f"4\n0:{doc_id}:{len(files)}:{size}\n" + "\n".join(lines) + "\n"
            h = hashlib.sha256(body.encode()).hexdigest()
            self.blobs[h] = body.encode()
            entries.append(f"{h}:80000000:{doc_id}:{len(files)}:{size}")
        body = "3\n" + "\n".join(entries) + "\n"  # an old-style v3 root, still readable
        self.root_hash = hashlib.sha256(body.encode()).hexdigest()
        self.blobs[self.root_hash] = body.encode()
        self.generation = 1

    # -- helpers for assertions ---------------------------------------------

    def documents(self) -> dict[str, dict[str, bytes]]:
        """Current cloud contents: doc id -> {file name: bytes}."""
        if not self.root_hash:
            return {}
        _schema, docs = parse_index(self.blobs[self.root_hash])
        out = {}
        for d in docs:
            _s, files = parse_index(self.blobs[d.hash])
            out[d.name] = {f.name: self.blobs[f.hash] for f in files}
        return out

    # -- HTTP ----------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        auth = request.headers.get("authorization", "")
        if request.method in ("POST", "PUT"):
            # Bodies are sent rmapi-style: octet-stream with a CRC32C header (even when empty).
            expected = "crc32c=" + base64.b64encode(struct.pack(">I", crc32c(request.content))).decode()
            if request.headers.get("x-goog-hash") != expected:
                return httpx.Response(400, text="bad or missing x-goog-hash")

        if path == "/token/json/2/device/new":
            body = json.loads(request.content)
            if body.get("code") != DEVICE_CODE or not body.get("deviceID"):
                return httpx.Response(400, text="invalid code")
            return httpx.Response(200, text=DEVICE_TOKEN)
        if path == "/token/json/2/user/new":
            if auth != f"Bearer {DEVICE_TOKEN}":
                return httpx.Response(401)
            token = fake_jwt(time.time() + self.token_lifetime)
            self.user_tokens.add(token)
            return httpx.Response(200, text=token)

        if not auth.startswith("Bearer ") or auth[7:] not in self.user_tokens:
            return httpx.Response(401)

        if path == "/sync/v4/root" and request.method == "GET":
            if not self.root_hash:
                return httpx.Response(404)
            return httpx.Response(200, json={"hash": self.root_hash, "generation": self.generation,
                                             "schemaVersion": 4})
        if path == "/sync/v3/root" and request.method == "PUT":
            return self._put_root(request)
        if path.startswith("/sync/v3/files/"):
            h = path.rsplit("/", 1)[1]
            if not request.headers.get("rm-filename"):
                return httpx.Response(400, text="missing rm-filename")
            if request.method == "GET":
                return httpx.Response(200, content=self.blobs[h]) if h in self.blobs else httpx.Response(404)
            if request.method == "PUT":
                return self._put_blob(h, request)
        return httpx.Response(404)

    def _put_blob(self, h: str, request: httpx.Request) -> httpx.Response:
        data = request.content
        name = request.headers["rm-filename"]
        if name == "root.docSchema":
            schema, entries = parse_index(data)
            lines = data.decode().split("\n")
            ids = [e.name for e in entries]
            if schema != "4" or ids != sorted(ids):
                return httpx.Response(400, json={"message": "invalid root schema"})
            if lines[1] != f"0:.:{len(entries)}:{sum(e.size for e in entries)}":
                return httpx.Response(400, json={"message": "invalid root schema header"})
            if hashlib.sha256(data).hexdigest() != h:
                return httpx.Response(400, text="root hash mismatch")
            for e in entries:
                if e.hash not in self.blobs:
                    return httpx.Response(400, text=f"missing doc index {e.name}")
        elif name.endswith(".docSchema"):
            schema, entries = parse_index(data)
            names = [e.name for e in entries]
            if names != sorted(names):
                return httpx.Response(400, text="unsorted doc index")
            concat = hashlib.sha256(b"".join(bytes.fromhex(e.hash) for e in entries)).hexdigest()
            if h not in (concat, hashlib.sha256(data).hexdigest()):
                return httpx.Response(400, text="doc index hash mismatch")
            for e in entries:
                if e.hash not in self.blobs:
                    return httpx.Response(400, text=f"missing file blob {e.name}")
                if len(self.blobs[e.hash]) != e.size:
                    return httpx.Response(400, text=f"wrong size for {e.name}")
        elif hashlib.sha256(data).hexdigest() != h:
            return httpx.Response(400, text="content hash mismatch")
        self.blobs[h] = data
        return httpx.Response(200)

    def _put_root(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("rm-filename") != "roothash":
            return httpx.Response(400, text="missing rm-filename")
        body = json.loads(request.content)
        if self.fail_next_root_puts:
            self.fail_next_root_puts -= 1
            self.generation += 1  # someone else (the tablet) won the race
            return httpx.Response(412, text="wrong generation")
        if body["generation"] != self.generation:
            return httpx.Response(412, text="wrong generation")
        if body["hash"] not in self.blobs:
            return httpx.Response(400, text="unknown root blob")
        self.root_hash = body["hash"]
        self.generation += 1
        self.root_puts += 1
        return httpx.Response(200, json={"hash": self.root_hash, "generation": self.generation})
