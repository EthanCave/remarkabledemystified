from __future__ import annotations

import json
import os
import stat

import pytest
from conftest import FOLDER_ID, NOTEBOOK_ID, PDF_ID, make_pdf
from fake_cloud import DEVICE_CODE, FakeCloud, fake_jwt

from remarkable_demystified.errors import AuthError, Conflict
from remarkable_demystified.library import Library
from remarkable_demystified.storage.cloud import (
    CloudAuth,
    CloudStorage,
    IndexEntry,
    crc32c,
    doc_index,
    goog_hash,
    parse_index,
    root_index,
)
from remarkable_demystified.storage.local import LocalStorage

HOST = "https://cloud.test"


@pytest.fixture
def cloud(xochitl_dir):
    fake = FakeCloud()
    fake.seed_from_dir(xochitl_dir)
    return fake


def make_storage(fake: FakeCloud, tmp_path, *, register: bool = True) -> CloudStorage:
    client = fake.client()
    auth = CloudAuth(tmp_path / "token.json", auth_host=HOST, client=client)
    if register:
        auth.register(DEVICE_CODE)
    return CloudStorage(auth, sync_host=HOST, cache_dir=tmp_path / "cache", client=client)


@pytest.fixture
def remote(cloud, tmp_path):
    return Library(make_storage(cloud, tmp_path))


# -- protocol primitives --------------------------------------------------------


def test_crc32c_known_vectors():
    assert crc32c(b"123456789") == 0xE3069283
    assert crc32c(b"") == 0
    assert goog_hash(b"123456789") == "crc32c=4waSgw=="


def test_index_serialization():
    files = [IndexEntry("bb" * 32, "0", "x.metadata", 0, 3), IndexEntry("aa" * 32, "0", "x.content", 0, 5)]
    h, body = doc_index(files)
    assert body.decode().splitlines()[0] == "3"
    assert body.decode().splitlines()[1].startswith("aa")  # sorted by file name
    assert parse_index(body)[1][0].name == "x.content"
    rh, rbody = root_index([IndexEntry(h, "0", "x", 2, 8)])
    assert rbody.decode().splitlines()[:2] == ["4", "0:.:1:8"]
    assert parse_index(rbody)[1] == [IndexEntry(h, "0", "x", 2, 8)]


# -- auth -------------------------------------------------------------------------


def test_register_and_token_refresh(cloud, tmp_path):
    client = cloud.client()
    auth = CloudAuth(tmp_path / "token.json", auth_host=HOST, client=client)
    assert not auth.registered
    with pytest.raises(AuthError):
        auth.register("zzzzzzzz")
    with pytest.raises(AuthError):
        auth.register("short")
    with pytest.raises(AuthError):
        auth.user_token()
    auth.register(DEVICE_CODE)
    assert stat.S_IMODE(os.stat(tmp_path / "token.json").st_mode) == 0o600
    first = auth.user_token()
    assert auth.user_token() == first  # cached while valid

    # an almost-expired token is renewed before use
    tokens = json.loads((tmp_path / "token.json").read_text())
    tokens["user_token"] = fake_jwt(0)
    (tmp_path / "token.json").write_text(json.dumps(tokens))
    reloaded = CloudAuth(tmp_path / "token.json", auth_host=HOST, client=client)
    assert reloaded.user_token() != fake_jwt(0)


def test_revoked_user_token_is_refreshed_on_401(cloud, tmp_path):
    lib = Library(make_storage(cloud, tmp_path))
    lib.items()
    cloud.user_tokens.clear()  # server forgets every session
    lib.refresh()
    assert lib.get(PDF_ID).name == "Paper"


# -- reading ------------------------------------------------------------------------


def test_listing_matches_local(remote, xochitl_dir):
    local = Library(LocalStorage(xochitl_dir))
    assert [i.to_dict() for i in remote.items(include_trash=True)] == [
        i.to_dict() for i in local.items(include_trash=True)
    ]
    assert remote.path(PDF_ID) == "/Books/Paper"


def test_reading_documents(remote, xochitl_dir):
    assert remote.source(PDF_ID)[0] == (xochitl_dir / f"{PDF_ID}.pdf").read_bytes()
    assert remote.export_pdf(PDF_ID).startswith(b"%PDF")
    assert "# Heading line" in remote.text(NOTEBOOK_ID).pages[0].text
    assert [p.has_annotations for p in remote.pages(PDF_ID)] == [True, False, True, False]


def test_mirror_cache_avoids_refetching(cloud, tmp_path):
    Library(make_storage(cloud, tmp_path)).items()
    cloud.requests.clear()
    again = Library(make_storage(cloud, tmp_path))  # new process, same cache dir
    assert again.get(PDF_ID).name == "Paper"
    gets = [p for m, p in cloud.requests if m == "GET" and p.startswith("/sync/v3/files/")]
    assert gets == []  # root unchanged: nothing downloaded


# -- writing ------------------------------------------------------------------------


def test_upload_and_edit_round_trip(remote, cloud):
    gen = cloud.generation
    doc = remote.upload(make_pdf(2), "Slides.pdf", parent=FOLDER_ID)
    assert cloud.generation == gen + 1
    files = cloud.documents()[doc.id]
    assert sorted(files) == sorted([f"{doc.id}.content", f"{doc.id}.metadata", f"{doc.id}.pdf"])
    assert json.loads(files[f"{doc.id}.metadata"])["parent"] == FOLDER_ID

    remote.rename(doc.id, "Slides (final)")
    assert json.loads(cloud.documents()[doc.id][f"{doc.id}.metadata"])["visibleName"] == "Slides (final)"

    # a second client sees the change
    other = Library(make_storage(cloud, remote.storage.cache_file.parent.parent / "other"))
    assert other.get(doc.id).name == "Slides (final)"

    remote.delete(doc.id, permanent=True)
    assert doc.id not in cloud.documents()


def test_notebook_and_folder_creation(remote, cloud):
    folder = remote.create_folder("Cloud folder")
    nb = remote.create_notebook("Typed", folder.id, text="hello cloud")
    stored = cloud.documents()
    assert folder.id in stored and nb.id in stored
    assert remote.text(nb.id).pages[0].text == "hello cloud\n"


def test_generation_conflict_is_retried(remote, cloud):
    remote.items()
    cloud.fail_next_root_puts = 2
    folder = remote.create_folder("Despite races")
    assert folder.id in cloud.documents()
    assert cloud.root_puts == 1


def test_persistent_conflict_raises(remote, cloud):
    cloud.fail_next_root_puts = 100
    with pytest.raises(Conflict):
        remote.create_folder("Never")
    assert cloud.root_puts == 0


def test_empty_account(tmp_path):
    fake = FakeCloud()
    lib = Library(make_storage(fake, tmp_path))
    assert lib.items() == []
    folder = lib.create_folder("First")
    assert list(fake.documents()) == [folder.id]
    assert lib.children("")[0].name == "First"
