import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# до импорта приложения: ключ подписи и отдельная база, чтобы тесты не трогали рабочие данные
os.environ["SECRET_KEY"] = "test-secret"
os.environ["APP_DB"] = str(Path(tempfile.mkdtemp()) / "bootstrap.sqlite")
os.environ.pop("RATING_API_URL", None)
os.environ.pop("PUBLIC_URL", None)

from fastapi.testclient import TestClient  # noqa: E402

from app import db as app_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB", str(tmp_path / "app.sqlite"))
    app_db.init_db()
    yield


@pytest.fixture
def db():
    conn = app_db.connect()
    yield conn
    conn.close()


@pytest.fixture
def new_client():
    """Каждый клиент — отдельный браузер со своими cookie."""
    clients = []

    def make(user_agent="pytest-browser"):
        client = TestClient(app, headers={"User-Agent": user_agent})
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


@pytest.fixture
def slugs(db):
    """Шесть настоящих вин из каталога с народным рейтингом."""
    rows = db.execute(
        "SELECT slug FROM cat.wines WHERE public_rating IS NOT NULL ORDER BY slug LIMIT 6"
    ).fetchall()
    return [r["slug"] for r in rows]


class Organizer:
    def __init__(self, client, email="org@example.com"):
        self.c = client
        r = client.post("/register", data={"name": "Винный клуб", "email": email, "password": "long-enough-pass"})
        assert r.status_code == 200, r.text

    def create_event(self, db, slugs, mode="open", access="link", seats="", preset=None, start=True):
        r = self.c.post("/org/events/new", data={"title": "Тестовая дегустация", "mode": mode, "access": access,
                                                 "seats": seats})
        assert r.status_code == 200, r.text
        event = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()
        for slug in slugs:
            self.c.post(f"/org/events/{event['id']}/wines/add", data={"slug": slug})
        if preset:
            self.c.post(f"/org/events/{event['id']}/scales/preset", data={"key": preset})
        if start:
            self.status(event["id"], "start")
        return self.event(db, event["id"])

    def status(self, event_id, action):
        return self.c.post(f"/org/events/{event_id}/status", data={"action": action})

    @staticmethod
    def event(db, event_id):
        db.commit()  # закрываем читающую транзакцию, чтобы увидеть свежие данные
        return db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


@pytest.fixture
def organizer(new_client):
    return Organizer(new_client())


def join(client, event, name="Гость", fp="iphone|ru|390x844", invite=""):
    return client.post(f"/j/{event['join_code']}", data={"name": name, "adult": "yes", "fp": fp, "invite": invite})


def wines_of(db, event):
    db.commit()
    return db.execute("SELECT * FROM event_wines WHERE event_id = ? ORDER BY position", (event["id"],)).fetchall()


def criteria_of(db, event):
    return db.execute("SELECT * FROM criteria WHERE event_id = ? ORDER BY position", (event["id"],)).fetchall()


def rate(client, db, event, wine, value, note=""):
    """Ставит одно и то же значение по всем критериям."""
    data = {f"c_{c['id']}": str(value) for c in criteria_of(db, event)}
    data["note"] = note
    return client.post(f"/t/{event['join_code']}/w/{wine['id']}", data=data)
