"""Read, edit and manage the documents on a reMarkable tablet.

Quick start::

    from remarkable_demystified import connect

    rm = connect(backend="ssh", ssh_password="...")   # or "cloud", "usb", "local"
    for item in rm.children(""):
        print(item.type, item.name)
"""

from .config import Settings, connect, load_settings
from .errors import (
    AuthError,
    Conflict,
    ConnectionFailed,
    InvalidRequest,
    NotFound,
    NotSupported,
    RemarkableError,
)
from .library import Library
from .model import DocumentText, Item, PageInfo

__version__ = "0.1.0"

__all__ = [
    "AuthError",
    "Conflict",
    "ConnectionFailed",
    "DocumentText",
    "InvalidRequest",
    "Item",
    "Library",
    "NotFound",
    "NotSupported",
    "PageInfo",
    "RemarkableError",
    "Settings",
    "connect",
    "load_settings",
]
