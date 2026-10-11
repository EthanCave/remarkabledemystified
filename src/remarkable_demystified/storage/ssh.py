"""Talk to the tablet directly over SSH (USB cable or Wi-Fi).

Only plain ``exec`` channels are used (``cat``, ``mv``, ``rm``, ``systemctl``),
never SFTP: several firmware versions ship dropbear without an SFTP server.

Writes follow the same recipe as every community tool: stop ``xochitl`` (the
tablet UI), write the files, start it again so it re-reads its library. Set
``restart="never"`` to skip that and restart the UI yourself later.
"""

from __future__ import annotations

import logging
import shlex
import threading
from collections.abc import Iterable
from functools import partial
from pathlib import Path
from typing import Any

import paramiko

from .. import xochitl
from ..errors import AuthError, ConnectionFailed, InvalidRequest, NotFound, RemarkableError
from .base import DocChange, DocumentRecord, DocumentStorage, resolved_writes

log = logging.getLogger(__name__)

DEFAULT_HOST = "10.11.99.1"  # the tablet's address over the USB cable
DEFAULT_XOCHITL_DIR = "/home/root/.local/share/remarkable/xochitl"

RS, US = b"\x1e", b"\x1f"  # record / unit separators: never appear raw in JSON


class SSHStorage(DocumentStorage):
    name = "ssh"
    cache_ttl = 5.0

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        *,
        username: str = "root",
        password: str | None = None,
        key_filename: str | None = None,
        port: int = 22,
        xochitl_dir: str = DEFAULT_XOCHITL_DIR,
        restart: str = "auto",
        known_hosts: str | Path | None = None,
        strict_host_key: bool = False,
        timeout: float = 10.0,
    ) -> None:
        super().__init__()
        if restart not in ("auto", "never"):
            raise InvalidRequest("restart must be 'auto' or 'never'")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.key_filename = key_filename
        self.dir = xochitl_dir.rstrip("/")
        self.restart = restart
        self.known_hosts = Path(known_hosts).expanduser() if known_hosts else None
        self.strict_host_key = strict_host_key
        self.timeout = timeout
        self._client: paramiko.SSHClient | None = None
        self._lock = threading.RLock()
        # name -> (mtime, size) and name -> parsed JSON, for incremental listing
        self._stat_cache: dict[str, tuple[str, str]] = {}
        self._json_cache: dict[str, dict[str, Any]] = {}

    # -- connection --------------------------------------------------------

    def _connect(self) -> paramiko.SSHClient:
        with self._lock:
            if self._client is not None:
                transport = self._client.get_transport()
                if transport is not None and transport.is_active():
                    return self._client
                self._client.close()
            client = paramiko.SSHClient()
            if self.known_hosts and self.known_hosts.exists():
                client.load_host_keys(str(self.known_hosts))
            client.set_missing_host_key_policy(
                paramiko.RejectPolicy() if self.strict_host_key else _TrustOnFirstUse()
            )
            try:
                client.connect(
                    self.host,
                    port=self.port,
                    username=self.username,
                    password=self.password,
                    key_filename=self.key_filename,
                    timeout=self.timeout,
                    banner_timeout=self.timeout,
                    auth_timeout=self.timeout,
                    allow_agent=self.password is None,
                    look_for_keys=self.password is None and self.key_filename is None,
                )
            except paramiko.BadHostKeyException as exc:
                raise ConnectionFailed(
                    f"The tablet's SSH host key changed ({exc}). This happens after a "
                    f"factory reset; remove {self.host} from {self.known_hosts} if expected."
                ) from exc
            except paramiko.AuthenticationException as exc:
                raise AuthError(
                    "SSH login refused. The root password is shown on the tablet under "
                    "Settings > Help > Copyrights and licenses (Paper Pro: enable developer mode)."
                ) from exc
            except paramiko.SSHException as exc:
                if self.strict_host_key and "not found in known_hosts" in str(exc):
                    raise ConnectionFailed(
                        f"{self.host} is not in {self.known_hosts} and strict host key checking is on. "
                        "Connect once with it off (over USB) or add the key with ssh-keyscan."
                    ) from exc
                raise ConnectionFailed(f"Cannot reach the tablet at {self.host}:{self.port}: {exc}") from exc
            except (TimeoutError, OSError) as exc:
                raise ConnectionFailed(f"Cannot reach the tablet at {self.host}:{self.port}: {exc}") from exc
            if self.known_hosts:
                self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
                client.save_host_keys(str(self.known_hosts))
            self._client = client
            return client

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def exec(self, command: str, stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
        """Run a shell command on the tablet; return ``(exit status, stdout, stderr)``."""
        client = self._connect()
        transport = client.get_transport()
        assert transport is not None
        try:
            chan = transport.open_session(timeout=self.timeout)
            chan.exec_command(command)
            if stdin is not None:
                chan.sendall(stdin)
            chan.shutdown_write()
            out = chan.makefile("rb").read()
            err = chan.makefile_stderr("rb").read()
            status = chan.recv_exit_status()
            chan.close()
        except (OSError, paramiko.SSHException) as exc:
            self.close()
            raise ConnectionFailed(f"SSH command failed: {exc}") from exc
        return status, out, err

    def run(self, command: str, stdin: bytes | None = None, *, check: bool = True) -> bytes:
        """Run a shell command on the tablet and return its stdout."""
        status, out, err = self.exec(command, stdin)
        if check and status != 0:
            message = err.decode(errors="replace").strip() or f"exit status {status}"
            raise RemarkableError(f"Command on tablet failed ({command.split()[0]}): {message}")
        return out

    # -- reading -----------------------------------------------------------

    def _path(self, name: str) -> str:
        return shlex.quote(f"{self.dir}/{name}")

    def _load_documents(self) -> dict[str, DocumentRecord]:
        cd = f"cd {shlex.quote(self.dir)} || exit 3"
        stats = self.run(f"{cd}; stat -c '%Y %s %n' -- *.metadata *.content 2>/dev/null; true")
        current: dict[str, tuple[str, str]] = {}
        for line in stats.decode(errors="replace").splitlines():
            parts = line.split(" ", 2)
            if len(parts) == 3:
                current[parts[2]] = (parts[0], parts[1])

        if not current:
            # Empty tablet, or no stat(1): read everything from scratch.
            self._json_cache = {}
            names = None
        else:
            names = [n for n, st in current.items() if self._stat_cache.get(n) != st]

        if names is None or names:
            loop = "printf '\\036%s\\037' \"$f\"; cat \"$f\""
            if names is None:
                blob = self.run(f"{cd}; for f in *.metadata *.content; do [ -f \"$f\" ] || continue; {loop}; done; true")
            else:
                # File names go over stdin: dropbear limits exec commands to ~9000 bytes.
                blob = self.run(
                    f"{cd}; while IFS= read -r f; do [ -f \"$f\" ] || continue; {loop}; done; true",
                    ("\n".join(names) + "\n").encode(),
                )
            for record in blob.split(RS)[1:]:
                raw_name, _, data = record.partition(US)
                self._json_cache[raw_name.decode(errors="replace")] = xochitl.loads(data)
        self._stat_cache = current

        if current:
            for name in list(self._json_cache):
                if name not in current:
                    del self._json_cache[name]  # deleted on the tablet
        records = {}
        for name, metadata in self._json_cache.items():
            if not name.endswith(".metadata"):
                continue
            doc_id = name[: -len(".metadata")]
            content = self._json_cache.get(xochitl.content_name(doc_id), {})
            records[doc_id] = DocumentRecord(doc_id, metadata, content)
        return records

    def list_files(self, doc_id: str) -> list[str]:
        _check_id(doc_id)
        out = self.run(
            f"cd {shlex.quote(self.dir)} && find {shlex.quote(doc_id)}* -type f 2>/dev/null; true"
        )
        names = [line for line in out.decode(errors="replace").splitlines() if line]
        return sorted(n for n in names if xochitl.doc_id_of(n) == doc_id and not n.endswith(".tmp"))

    def read_file(self, doc_id: str, name: str) -> bytes:
        _check_name(doc_id, name)
        status, out, _err = self.exec(f"cat {self._path(name)}")
        if status != 0:
            raise NotFound(f"{name} does not exist on the tablet")
        return out

    def read_files(self, doc_id: str, names: Iterable[str]) -> dict[str, bytes]:
        """Fetch many files in one round trip (length-prefixed ``cat`` over one channel)."""
        names = list(dict.fromkeys(names))
        for name in names:
            _check_name(doc_id, name)
        if len(names) <= 1:
            return {name: self.read_file(doc_id, name) for name in names}
        frame = (
            'if [ -f "$f" ]; then printf \'%s %s\\n\' "$(($(wc -c < "$f")))" "$f"; cat "$f"; '
            'else printf \'%s %s\\n\' -1 "$f"; fi'
        )
        out = self.run(
            f"cd {shlex.quote(self.dir)} || exit 3; while IFS= read -r f; do {frame}; done",
            ("\n".join(names) + "\n").encode(),
        )
        result: dict[str, bytes] = {}
        pos = 0
        try:
            while pos < len(out):
                nl = out.index(b"\n", pos)
                size_s, _, name = out[pos:nl].decode().partition(" ")
                size = int(size_s)
                pos = nl + 1
                if size < 0:
                    raise NotFound(f"{name} does not exist on the tablet")
                result[name] = out[pos : pos + size]
                pos += size
        except (ValueError, UnicodeDecodeError):
            log.warning("garbled multi-file read; falling back to one file at a time")
            return {name: self.read_file(doc_id, name) for name in names}
        missing = [n for n in names if n not in result]
        for name in missing:
            result[name] = self.read_file(doc_id, name)
        return result

    # -- writing -----------------------------------------------------------

    def _apply(self, changes: list[DocChange]) -> None:
        stopped = False
        if self.restart == "auto":
            self.run("systemctl stop xochitl", check=False)
            stopped = True
        try:
            for change in changes:
                _check_id(change.doc_id)
                if change.delete:
                    doc = shlex.quote(change.doc_id)
                    self.run(f"cd {shlex.quote(self.dir)} && rm -rf -- {doc} {doc}.*")
                    continue
                if change.remove:
                    for name in change.remove:
                        _check_name(change.doc_id, name)
                    self.run(
                        f"cd {shlex.quote(self.dir)} && while IFS= read -r f; do rm -f \"$f\"; done",
                        ("\n".join(sorted(change.remove)) + "\n").encode(),
                    )
                # Patches read the files now that xochitl has stopped and saved its state.
                writes = resolved_writes(change, partial(self._read_or_none, change.doc_id))
                ordered = sorted(writes.items(), key=lambda kv: kv[0].endswith(".metadata"))
                for name, data in ordered:
                    _check_name(change.doc_id, name)
                    final = f"{self.dir}/{name}"
                    parent = shlex.quote(final.rsplit("/", 1)[0])
                    tmp = shlex.quote(final + ".tmp")
                    self.run(f"mkdir -p {parent} && cat > {tmp} && mv -f {tmp} {shlex.quote(final)}", data)
        finally:
            if stopped:
                self.run("systemctl start xochitl", check=False)

    def _read_or_none(self, doc_id: str, name: str) -> bytes | None:
        try:
            return self.read_file(doc_id, name)
        except NotFound:
            return None

    def restart_ui(self) -> None:
        """Restart xochitl so it picks up files changed behind its back."""
        self.run("systemctl restart xochitl")

    # -- device ------------------------------------------------------------

    def device_info(self) -> dict[str, Any]:
        script = r"""
echo "model=$(cat /sys/devices/soc0/machine 2>/dev/null | tr -d '\0')"
echo "hostname=$(hostname)"
grep -h -E '^(REMARKABLE_RELEASE_VERSION|IMG_VERSION)=' /usr/share/remarkable/update.conf /etc/os-release 2>/dev/null | head -n1 | sed 's/^[A-Z_]*=/firmware=/; s/"//g'
for d in /sys/class/power_supply/*; do
  if [ "$(cat "$d/type" 2>/dev/null)" = Battery ]; then
    echo "battery_percent=$(cat "$d/capacity" 2>/dev/null)"
    echo "battery_status=$(cat "$d/status" 2>/dev/null)"
    break
  fi
done
df -k /home 2>/dev/null | tail -n1 | awk '{print "storage_total_kb=" $2; print "storage_used_kb=" $3; print "storage_free_kb=" $4}'
echo "uptime_seconds=$(cut -d' ' -f1 /proc/uptime 2>/dev/null)"
echo "xochitl=$(systemctl is-active xochitl 2>/dev/null)"
"""
        info: dict[str, Any] = {"backend": self.name, "host": self.host}
        out = self.run(script, check=False).decode(errors="replace")
        for line in out.splitlines():
            key, sep, value = line.partition("=")
            if not sep or not value.strip():
                continue
            value = value.strip()
            if key in ("battery_percent", "storage_total_kb", "storage_used_kb", "storage_free_kb"):
                try:
                    info[key] = int(value)
                    continue
                except ValueError:
                    pass
            if key == "uptime_seconds":
                try:
                    info[key] = float(value)
                    continue
                except ValueError:
                    pass
            info[key] = value
        return info


class _TrustOnFirstUse(paramiko.MissingHostKeyPolicy):
    """Accept an unknown tablet's host key, and say so with its fingerprint (like ssh does).

    Compare it with ``ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`` on the tablet,
    or set ``ssh_strict_host_key`` to refuse unknown keys altogether.
    """

    def missing_host_key(self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey) -> None:
        log.warning(
            "trusting new SSH host key for %s: %s %s", hostname, key.get_name(), key.fingerprint
        )
        client.get_host_keys().add(hostname, key.get_name(), key)


def _check_id(doc_id: str) -> None:
    if not doc_id or any(c in doc_id for c in "/.*?[]\\ \n'\"$`") or doc_id == xochitl.TRASH:
        raise InvalidRequest(f"invalid document id {doc_id!r}")


def _check_name(doc_id: str, name: str) -> None:
    _check_id(doc_id)
    if not xochitl.is_safe_file_name(doc_id, name):
        raise InvalidRequest(f"{name!r} is not a file of document {doc_id!r}")
