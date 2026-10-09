"""A xochitl directory on the local filesystem.

Useful for a backup made with ``scp -r root@10.11.99.1:.local/share/remarkable/xochitl .``,
for tests, and as the reference implementation of :class:`DocumentStorage`.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .. import xochitl
from ..errors import InvalidRequest, NotFound
from .base import DocChange, DocumentRecord, DocumentStorage


def _safe_join(root: Path, doc_id: str, name: str) -> Path:
    """Resolve ``name`` inside ``root`` and make sure it belongs to ``doc_id``."""
    if xochitl.doc_id_of(name) != doc_id or ".." in Path(name).parts or name.startswith("/"):
        raise InvalidRequest(f"{name!r} is not a file of document {doc_id!r}")
    return root / name


class LocalStorage(DocumentStorage):
    name = "local"

    def __init__(self, root: str | os.PathLike[str]) -> None:
        super().__init__()
        self.root = Path(root).expanduser()
        if not self.root.is_dir():
            raise NotFound(f"xochitl directory {self.root} does not exist")

    def _load_documents(self) -> dict[str, DocumentRecord]:
        records = {}
        for meta_path in self.root.glob("*.metadata"):
            doc_id = meta_path.name[: -len(".metadata")]
            metadata = xochitl.loads(meta_path.read_bytes())
            content_path = self.root / xochitl.content_name(doc_id)
            content = xochitl.loads(content_path.read_bytes()) if content_path.exists() else {}
            records[doc_id] = DocumentRecord(doc_id, metadata, content)
        return records

    def list_files(self, doc_id: str) -> list[str]:
        names = []
        for path in self.root.glob(f"{doc_id}*"):
            if xochitl.doc_id_of(path.name) != doc_id:
                continue
            if path.is_dir():
                names.extend(
                    str(p.relative_to(self.root)).replace(os.sep, "/")
                    for p in path.rglob("*")
                    if p.is_file()
                )
            else:
                names.append(path.name)
        return sorted(names)

    def read_file(self, doc_id: str, name: str) -> bytes:
        path = _safe_join(self.root, doc_id, name)
        try:
            return path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            raise NotFound(f"{name} does not exist") from None

    def _apply(self, changes: list[DocChange]) -> None:
        for change in changes:
            if change.delete:
                for name in self.list_files(change.doc_id):
                    (self.root / name).unlink(missing_ok=True)
                for path in self.root.glob(f"{change.doc_id}*"):
                    if path.is_dir() and xochitl.doc_id_of(path.name) == change.doc_id:
                        shutil.rmtree(path, ignore_errors=True)
                continue
            for name in change.remove:
                _safe_join(self.root, change.doc_id, name).unlink(missing_ok=True)
            # Write .metadata last so a half-written document never shows up.
            for name, data in sorted(change.write.items(), key=lambda kv: kv[0].endswith(".metadata")):
                path = _safe_join(self.root, change.doc_id, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, path)

    def device_info(self) -> dict[str, object]:
        return {"backend": self.name, "path": str(self.root)}
