"""Exception hierarchy shared by the library, the CLI and the REST server."""

from __future__ import annotations


class RemarkableError(Exception):
    """Base class for every error raised by this package."""

    status_code = 500


class NotFound(RemarkableError):
    """The requested document, folder, page or file does not exist."""

    status_code = 404


class InvalidRequest(RemarkableError):
    """The request is malformed (bad name, moving a folder into itself, ...)."""

    status_code = 400


class NotSupported(RemarkableError):
    """The active backend cannot perform this operation (e.g. rename over USB web)."""

    status_code = 501


class Conflict(RemarkableError):
    """The tablet or cloud changed underneath us and the change could not be applied."""

    status_code = 409


class AuthError(RemarkableError):
    """Authentication with the tablet or the reMarkable cloud failed."""

    status_code = 401


class ConnectionFailed(RemarkableError):
    """The tablet (or cloud) could not be reached."""

    status_code = 502
