"""End-to-end tests of the SSH backend against a real OpenSSH server.

Skipped unless ``sshd`` and ``ssh-keygen`` are installed. The server runs on a
random localhost port with a throwaway host key, no SFTP subsystem (like the
tablet's dropbear) and a fake ``systemctl`` that records how xochitl is managed.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from conftest import NOTEBOOK_ID, PDF_ID, build_library, make_pdf

from remarkable_demystified.errors import AuthError, NotFound
from remarkable_demystified.library import Library
from remarkable_demystified.storage.local import LocalStorage
from remarkable_demystified.storage.ssh import SSHStorage

SSHD = shutil.which("sshd") or ("/usr/sbin/sshd" if os.path.exists("/usr/sbin/sshd") else None)
KEYGEN = shutil.which("ssh-keygen")

pytestmark = pytest.mark.skipif(not (SSHD and KEYGEN), reason="OpenSSH server not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def sshd(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sshd")
    subprocess.run([KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(tmp / "host")], check=True)
    subprocess.run([KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(tmp / "client")], check=True)
    subprocess.run([KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(tmp / "stranger")], check=True)
    shutil.copy(tmp / "client.pub", tmp / "authorized_keys")
    bindir = tmp / "bin"
    bindir.mkdir()
    fake = bindir / "systemctl"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {tmp}/systemctl.log\n[ "$1" = is-active ] && echo active\nexit 0\n')
    fake.chmod(0o755)
    if os.geteuid() == 0:
        Path("/run/sshd").mkdir(exist_ok=True)
    port = _free_port()
    config = tmp / "sshd_config"
    config.write_text(
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {tmp}/host\n"
        f"AuthorizedKeysFile {tmp}/authorized_keys\nPasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\nPubkeyAuthentication yes\nPermitRootLogin yes\n"
        f"StrictModes no\nPidFile {tmp}/sshd.pid\nUsePAM no\n"
        f"SetEnv PATH={bindir}:/usr/local/bin:/usr/bin:/bin\n"
    )
    proc = subprocess.Popen([SSHD, "-D", "-e", "-f", str(config)], stderr=subprocess.PIPE)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            if proc.poll() is not None:
                pytest.skip(f"sshd failed to start: {proc.stderr.read().decode()[-500:]}")
            time.sleep(0.05)
    yield {"port": port, "tmp": tmp, "key": str(tmp / "client"), "stranger": str(tmp / "stranger"),
           "log": tmp / "systemctl.log"}
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def remote(sshd, tmp_path):
    root = build_library(tmp_path / "xochitl")
    if sshd["log"].exists():
        sshd["log"].unlink()
    storage = SSHStorage(
        "127.0.0.1", port=sshd["port"], username=getpass.getuser(),
        key_filename=sshd["key"], xochitl_dir=str(root), known_hosts=tmp_path / "known_hosts",
    )
    yield Library(storage), root, sshd["log"]
    storage.close()


def test_listing_matches_local(remote):
    lib, root, _ = remote
    local = Library(LocalStorage(root))
    assert [i.to_dict() for i in lib.items(include_trash=True)] == [
        i.to_dict() for i in local.items(include_trash=True)
    ]
    assert lib.path(PDF_ID) == "/Books/Paper"
    assert (root.parent / "known_hosts").exists()  # trust-on-first-use host key saved


def test_incremental_refresh_sees_tablet_changes(remote):
    lib, root, _ = remote
    assert lib.get(PDF_ID).name == "Paper"
    meta = json.loads((root / f"{PDF_ID}.metadata").read_text())
    meta["visibleName"] = "Renamed on tablet"
    time.sleep(1.1)  # stat(1) has 1-second mtime resolution
    (root / f"{PDF_ID}.metadata").write_text(json.dumps(meta))
    for p in root.glob(f"{NOTEBOOK_ID}*"):
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    lib.refresh()
    assert lib.get(PDF_ID).name == "Renamed on tablet"
    with pytest.raises(NotFound):
        lib.get(NOTEBOOK_ID)


def test_binary_reads(remote):
    lib, root, _ = remote
    names = [f"{PDF_ID}/p-a.rm", f"{PDF_ID}/p-c.rm", f"{PDF_ID}.pdf"]
    got = lib.storage.read_files(PDF_ID, names)
    assert got == {n: (root / n).read_bytes() for n in names}
    assert lib.files(PDF_ID) == LocalStorage(root).list_files(PDF_ID)
    with pytest.raises(NotFound):
        lib.read_file(PDF_ID, f"{PDF_ID}.epub")


def test_writes_stop_and_restart_xochitl(remote):
    lib, root, log = remote
    doc = lib.upload(make_pdf(2), "Uploaded.pdf")
    assert (root / f"{doc.id}.pdf").read_bytes().startswith(b"%PDF")
    assert not list(root.glob("*.tmp"))
    assert log.read_text().splitlines() == ["stop xochitl", "start xochitl"]
    lib.rename(doc.id, "Renamed")
    assert lib.get(doc.id).name == "Renamed"
    lib.delete(doc.id, permanent=True)
    assert not list(root.glob(f"{doc.id}*"))
    nb = lib.create_notebook("Typed", text="# Hello\nfrom the API")
    assert lib.text(nb.id).pages[0].text == "# Hello\nfrom the API\n"


def test_render_and_export_over_ssh(remote):
    lib, _, _ = remote
    assert lib.export_pdf(PDF_ID).startswith(b"%PDF")
    png, _ = lib.render_page(NOTEBOOK_ID, 1, "png", scale=0.2)
    assert png.startswith(b"\x89PNG")


def test_device_info(remote):
    lib, _, _ = remote
    info = lib.device_info()
    assert info["backend"] == "ssh" and info["xochitl"] == "active"
    assert "storage_free_kb" in info and "hostname" in info


def test_wrong_key_is_auth_error(sshd, tmp_path):
    storage = SSHStorage("127.0.0.1", port=sshd["port"], username=getpass.getuser(),
                         key_filename=sshd["stranger"], xochitl_dir=str(tmp_path))
    with pytest.raises(AuthError):
        storage.run("true")
