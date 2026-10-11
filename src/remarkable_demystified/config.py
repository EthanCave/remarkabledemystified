"""Settings: defaults < config file < environment variables < explicit overrides.

Config file (``~/.config/remarkable-demystified/config.toml``, or ``$RMD_CONFIG``)::

    backend = "ssh"            # ssh | cloud | usb | local

    [ssh]
    host = "10.11.99.1"        # USB cable; use the tablet's Wi-Fi IP otherwise
    password = "abc123"        # Settings > Help > Copyrights and licenses
    # key = "~/.ssh/id_ed25519"

    [server]
    api_token = "long-random-string"

Every setting also has an ``RMD_*`` environment variable, e.g. ``RMD_BACKEND``,
``RMD_SSH_HOST``, ``RMD_SSH_PASSWORD``, ``RMD_API_TOKEN``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from .errors import InvalidRequest

APP_NAME = "remarkable-demystified"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / APP_NAME


BACKENDS = ("ssh", "cloud", "usb", "local")


@dataclass
class Settings:
    backend: str = "ssh"

    ssh_host: str = "10.11.99.1"
    ssh_port: int = 22
    ssh_user: str = "root"
    ssh_password: str | None = None
    ssh_key: str | None = None
    ssh_restart: str = "auto"
    #: Refuse tablets whose host key is not already in known_hosts (no trust-on-first-use).
    ssh_strict_host_key: bool = False
    xochitl_dir: str = "/home/root/.local/share/remarkable/xochitl"
    known_hosts: str = field(default_factory=lambda: str(config_dir() / "known_hosts"))

    local_dir: str | None = None

    cloud_token_file: str = field(default_factory=lambda: str(config_dir() / "cloud-token.json"))
    cloud_auth_host: str = "https://webapp-prod.cloud.remarkable.engineering"
    cloud_sync_host: str = "https://internal.cloud.remarkable.com"
    cache_dir: str = field(default_factory=lambda: str(cache_dir()))

    usb_url: str = "http://10.11.99.1"

    api_token: str | None = None
    max_upload_mb: int = 512

    def redacted(self) -> dict[str, Any]:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        for secret in ("ssh_password", "api_token"):
            if out[secret]:
                out[secret] = "***"
        return out


# config-file section/key -> Settings field
_FILE_KEYS = {
    ("", "backend"): "backend",
    ("ssh", "host"): "ssh_host",
    ("ssh", "port"): "ssh_port",
    ("ssh", "user"): "ssh_user",
    ("ssh", "password"): "ssh_password",
    ("ssh", "key"): "ssh_key",
    ("ssh", "restart"): "ssh_restart",
    ("ssh", "xochitl_dir"): "xochitl_dir",
    ("ssh", "known_hosts"): "known_hosts",
    ("ssh", "strict_host_key"): "ssh_strict_host_key",
    ("local", "dir"): "local_dir",
    ("cloud", "token_file"): "cloud_token_file",
    ("cloud", "auth_host"): "cloud_auth_host",
    ("cloud", "sync_host"): "cloud_sync_host",
    ("cloud", "cache_dir"): "cache_dir",
    ("usb", "url"): "usb_url",
    ("server", "api_token"): "api_token",
    ("server", "max_upload_mb"): "max_upload_mb",
}


def _coerce(name: str, value: Any) -> Any:
    if name == "ssh_strict_host_key":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        raise InvalidRequest(f"{name} must be true or false, got {value!r}")
    if name in ("ssh_port", "max_upload_mb"):
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise InvalidRequest(f"{name} must be a number, got {value!r}") from exc
    return None if value == "" else str(value)


def config_file_path(config_file: str | os.PathLike[str] | None = None) -> Path:
    return Path(config_file or os.environ.get("RMD_CONFIG") or config_dir() / "config.toml").expanduser()


def read_config_file(config_file: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """The raw TOML of the config file, or ``{}`` if there is none."""
    path = config_file_path(config_file)
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise InvalidRequest(f"{path} is not valid TOML: {exc}") from exc


def load_settings(config_file: str | os.PathLike[str] | None = None, **overrides: Any) -> Settings:
    settings = Settings()
    path = config_file_path(config_file)
    if path.exists():
        data = read_config_file(config_file)
        for (section, key), name in _FILE_KEYS.items():
            scope = data if not section else data.get(section, {})
            if isinstance(scope, dict) and key in scope:
                setattr(settings, name, _coerce(name, scope[key]))
    elif config_file:
        raise InvalidRequest(f"config file {path} does not exist")

    for f in fields(Settings):
        env = os.environ.get(f"RMD_{f.name.upper()}")
        if env is not None:
            setattr(settings, f.name, _coerce(f.name, env))

    for name, value in overrides.items():
        if value is None:
            continue
        if not hasattr(settings, name):
            raise TypeError(f"unknown setting {name!r}")
        setattr(settings, name, _coerce(name, value) if name != "backend" else value)

    if settings.backend not in BACKENDS:
        raise InvalidRequest(f"backend must be one of {', '.join(BACKENDS)}")
    return settings


def connect(settings: Settings | None = None, **overrides: Any):
    """Open a :class:`~remarkable_demystified.library.Library` for the configured backend."""
    from .library import Library

    if settings is None:
        settings = load_settings(**overrides)
    elif overrides:
        raise TypeError("pass either settings or overrides, not both")

    if settings.backend == "ssh":
        from .storage.ssh import SSHStorage

        return Library(SSHStorage(
            settings.ssh_host,
            port=settings.ssh_port,
            username=settings.ssh_user,
            password=settings.ssh_password,
            key_filename=os.path.expanduser(settings.ssh_key) if settings.ssh_key else None,
            xochitl_dir=settings.xochitl_dir,
            restart=settings.ssh_restart,
            known_hosts=settings.known_hosts,
            strict_host_key=settings.ssh_strict_host_key,
        ))
    if settings.backend == "cloud":
        from .storage.cloud import CloudAuth, CloudStorage

        auth = CloudAuth(settings.cloud_token_file, auth_host=settings.cloud_auth_host)
        return Library(CloudStorage(auth, sync_host=settings.cloud_sync_host, cache_dir=settings.cache_dir))
    if settings.backend == "usb":
        from .storage.usbweb import UsbWebLibrary, UsbWebStorage

        return UsbWebLibrary(UsbWebStorage(settings.usb_url))
    from .storage.local import LocalStorage

    if not settings.local_dir:
        raise InvalidRequest("the local backend needs local_dir (RMD_LOCAL_DIR)")
    return Library(LocalStorage(settings.local_dir))
