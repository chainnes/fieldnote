import pytest
from fastapi.testclient import TestClient

from fieldnote.config import Settings
from fieldnote.db import Store
from fieldnote.service import Desk


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        provider="mock",
        model="fieldnote-rules",
        api_key="",
        base_url="",
        temperature=0,
        db_path=tmp_path / "fieldnote.db",
        seed_dir=__import__("pathlib").Path(__file__).resolve().parents[1] / "fieldnote" / "seed",
        as_of=__import__("datetime").date(2026, 3, 25),
        host="127.0.0.1",
        port=43123,
    )


@pytest.fixture()
def store(settings):
    opened = Store(settings.db_path, settings.seed_dir, settings.as_of)
    yield opened
    opened.close()


@pytest.fixture()
def desk(settings, store):
    return Desk(settings, store)


@pytest.fixture()
def client(settings, store):
    from fieldnote.app import create_app

    app = create_app(settings, store)
    with TestClient(app) as test_client:
        yield test_client
