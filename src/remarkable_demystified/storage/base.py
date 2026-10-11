"""The file-level storage interface that every full-featured backend implements.

The tablet's own directory (over SSH), a local copy of it, and the reMarkable
cloud all store the same thing: for every document id, a small set of files
(``<id>.metadata``, ``<id>.content``, ``<id>/<page>.rm``, ...). A
:class:`DocumentStorage` exposes exactly that, and :class:`~remarkable_demystified.library.Library`
builds every higher-level operation (rename, upload, render, ...) on top of it.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .. import xochitl


@dataclass
class DocumentRecord:
    """The parsed ``.metadata`` and ``.content`` of one document or folder."""

    id: str
    metadata: dict[str, Any]
    content: dict[str, Any]


JsonPatch = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class DocChange:
    """A change to one document, applied atomically by :meth:`DocumentStorage.apply`.

    ``write`` maps file names (relative to the xochitl directory, e.g.
    ``"<id>.metadata"`` or ``"<id>/<page>.rm"``) to their new bytes. ``remove``
    lists files to delete. ``delete`` removes the whole document.

    ``patch`` maps JSON file names (``.metadata``, ``.content``) to functions that
    turn the file's *current* contents into the new ones. Storages run them at
    the last moment (over SSH: after the tablet UI has stopped and saved), so an
    edit never overwrites something the tablet wrote in the meantime.
    """

    doc_id: str
    write: dict[str, bytes] = field(default_factory=dict)
    remove: set[str] = field(default_factory=set)
    delete: bool = False
    patch: dict[str, JsonPatch] = field(default_factory=dict)


def resolved_writes(change: DocChange, read_current: Callable[[str], bytes | None]) -> dict[str, bytes]:
    """``change.write`` plus the result of each patch applied to the file as it is now."""
    writes = dict(change.write)
    for name, fn in change.patch.items():
        writes[name] = xochitl.dumps(fn(xochitl.loads(read_current(name))))
    return writes


class DocumentStorage(ABC):
    """Abstract per-document file store. Implementations: local, SSH, cloud."""

    #: Short backend name reported by the API (``"local"``, ``"ssh"``, ``"cloud"``).
    name: str = "storage"

    #: Seconds a document listing may be served from memory before re-reading it.
    cache_ttl: float = 0.0

    def __init__(self) -> None:
        self._records: dict[str, DocumentRecord] | None = None
        self._records_at = 0.0

    # -- reading -----------------------------------------------------------

    def documents(self, *, refresh: bool = False) -> dict[str, DocumentRecord]:
        """All documents and folders, keyed by id (cached for :attr:`cache_ttl`)."""
        fresh = time.monotonic() - self._records_at < self.cache_ttl
        if refresh or self._records is None or not fresh:
            self._records = self._load_documents()
            self._records_at = time.monotonic()
        return self._records

    def invalidate(self) -> None:
        self._records = None

    @abstractmethod
    def _load_documents(self) -> dict[str, DocumentRecord]:
        """Read every ``.metadata`` (and ``.content``) file."""

    @abstractmethod
    def list_files(self, doc_id: str) -> list[str]:
        """Names of every file belonging to ``doc_id``, relative to the xochitl dir."""

    @abstractmethod
    def read_file(self, doc_id: str, name: str) -> bytes:
        """Raw bytes of one file. Raises :class:`~remarkable_demystified.errors.NotFound`."""

    def read_files(self, doc_id: str, names: Iterable[str]) -> dict[str, bytes]:
        return {name: self.read_file(doc_id, name) for name in names}

    # -- writing -----------------------------------------------------------

    def apply(self, changes: Iterable[DocChange]) -> None:
        """Apply a batch of document changes, then refresh the cached listing."""
        changes = [c for c in changes if c.write or c.remove or c.delete or c.patch]
        if not changes:
            return
        try:
            self._apply(changes)
        finally:
            self.invalidate()

    @abstractmethod
    def _apply(self, changes: list[DocChange]) -> None: ...

    # -- misc --------------------------------------------------------------

    def device_info(self) -> dict[str, Any]:
        """Backend-specific facts (battery, firmware, cloud generation, ...)."""
        return {"backend": self.name}

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release connections."""

    def __enter__(self) -> DocumentStorage:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def merge_records(
    metadata: Mapping[str, dict[str, Any]], content: Mapping[str, dict[str, Any]]
) -> dict[str, DocumentRecord]:
    return {
        doc_id: DocumentRecord(doc_id, meta, dict(content.get(doc_id) or {}))
        for doc_id, meta in metadata.items()
    }
