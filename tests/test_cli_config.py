from __future__ import annotations

import json

import pytest
from conftest import PDF_ID, make_pdf

from remarkable_demystified.cli import main
from remarkable_demystified.config import connect, load_settings
from remarkable_demystified.errors import InvalidRequest
from remarkable_demystified.storage.ssh import SSHStorage


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for key in list(__import__("os").environ):
        if key.startswith("RMD_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def rmd(xochitl_dir, *args):
    return main(["--backend", "local", "--local-dir", str(xochitl_dir), *args])


def test_ls_tree_info(xochitl_dir, capsys):
    assert rmd(xochitl_dir, "ls") == 0
    assert capsys.readouterr().out.splitlines() == ["Books/", "Legacy sketch", "Notes"]
    assert rmd(xochitl_dir, "ls", "-l", "/Books") == 0
    out = capsys.readouterr().out
    assert "pdf     4p 2023-11-14" in out and f"[{PDF_ID}]" in out
    assert rmd(xochitl_dir, "ls", "--json", "/Books/Paper") == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == PDF_ID
    assert rmd(xochitl_dir, "tree") == 0
    assert "├── Books/\n│   ├── Archive/\n│   └── Paper" in capsys.readouterr().out
    assert rmd(xochitl_dir, "info") == 0
    assert json.loads(capsys.readouterr().out)["backend"] == "local"


def test_get_put_mv_rm(xochitl_dir, tmp_path, capsys):
    out = tmp_path / "paper.pdf"
    assert rmd(xochitl_dir, "get", "/Books/Paper", "-o", str(out)) == 0
    assert out.read_bytes().startswith(b"%PDF")
    assert rmd(xochitl_dir, "get", "/Books/Paper", "-f", "original", "-o", str(tmp_path)) == 0
    assert (tmp_path / "Paper.pdf").exists()

    src = tmp_path / "lecture.pdf"
    src.write_bytes(make_pdf(1))
    assert rmd(xochitl_dir, "put", str(src), "--to", "/School/Fall", "--mkdir") == 0
    assert "uploaded /School/Fall/lecture" in capsys.readouterr().out

    assert rmd(xochitl_dir, "mv", "/School/Fall/lecture", "/Books") == 0
    assert capsys.readouterr().out.strip() == "/Books/lecture"
    assert rmd(xochitl_dir, "mv", "/Books/lecture", "/School/Lecture 1") == 0
    assert capsys.readouterr().out.strip() == "/School/Lecture 1"

    assert rmd(xochitl_dir, "rm", "/School/Lecture 1") == 0
    assert rmd(xochitl_dir, "restore", "/Trash/Lecture 1", "--to", "/Books") == 0
    assert capsys.readouterr().out.splitlines()[-1] == "/Books/Lecture 1"
    assert rmd(xochitl_dir, "rm", "/School") == 0
    assert rmd(xochitl_dir, "empty-trash") == 0
    assert "deleted 3 item(s)" in capsys.readouterr().out  # Old draft + School + Fall


def test_text_note_render_tags(xochitl_dir, tmp_path, capsys):
    assert rmd(xochitl_dir, "note", "Plan", "--text", "# Plan\\n- [ ] ship", "--to", "/Work") == 0
    capsys.readouterr()
    assert rmd(xochitl_dir, "text", "/Work/Plan") == 0
    assert "# Plan\n- [ ] ship" in capsys.readouterr().out
    png = tmp_path / "p.png"
    assert rmd(xochitl_dir, "render", "/Notes", "--page", "2", "--scale", "0.2", "-o", str(png)) == 0
    assert png.read_bytes().startswith(b"\x89PNG")
    assert rmd(xochitl_dir, "tag", "/Notes", "a", "b") == 0
    assert capsys.readouterr().out.strip() == "a, b"
    assert rmd(xochitl_dir, "pin", "/Books/Paper") == 0
    assert rmd(xochitl_dir, "pages", "/Books/Paper") == 0
    assert "inserted" not in capsys.readouterr().out.splitlines()[0]


def test_errors_exit_nonzero(xochitl_dir, capsys):
    assert rmd(xochitl_dir, "ls", "/Nope") == 1
    assert "nothing called 'Nope'" in capsys.readouterr().err
    assert rmd(xochitl_dir, "restart-ui") == 1


def test_settings_precedence(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('backend = "ssh"\n[ssh]\nhost = "192.168.1.50"\npassword = "filepw"\nport = 2222\n'
                   '[server]\napi_token = "tok"\n')
    s = load_settings(cfg)
    assert (s.ssh_host, s.ssh_password, s.ssh_port, s.api_token) == ("192.168.1.50", "filepw", 2222, "tok")
    monkeypatch.setenv("RMD_SSH_PASSWORD", "envpw")
    monkeypatch.setenv("RMD_SSH_PORT", "22")
    s = load_settings(cfg, ssh_host="10.0.0.9")
    assert (s.ssh_host, s.ssh_password, s.ssh_port) == ("10.0.0.9", "envpw", 22)
    assert s.redacted()["ssh_password"] == "***"
    with pytest.raises(InvalidRequest):
        load_settings(tmp_path / "missing.toml")
    with pytest.raises(InvalidRequest):
        load_settings(cfg, backend="bluetooth")
    monkeypatch.setenv("RMD_SSH_PORT", "abc")
    with pytest.raises(InvalidRequest):
        load_settings(cfg)


def test_connect_builds_backends(tmp_path, xochitl_dir):
    lib = connect(backend="ssh", ssh_host="192.0.2.1", ssh_password="pw")
    assert isinstance(lib.storage, SSHStorage) and lib.storage.host == "192.0.2.1"
    assert connect(backend="local", local_dir=str(xochitl_dir)).backend == "local"
    assert connect(backend="usb").backend == "usb"
    assert connect(backend="cloud").backend == "cloud"
    with pytest.raises(InvalidRequest):
        connect(backend="local")
