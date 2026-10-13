"""Подключение к SQLite и схема данных приложения.

Каталог вин (data/catalog.sqlite) подключается только на чтение под именем cat:
приложение ссылается на вина по slug карточки «Своё Вино» и ничего в каталоге не меняет.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

SCHEMA = """
CREATE TABLE IF NOT EXISTS organizers (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    organizer_id INTEGER NOT NULL REFERENCES organizers,
    title        TEXT NOT NULL,
    description  TEXT,
    place        TEXT,
    starts_at    TEXT,
    mode         TEXT NOT NULL CHECK (mode IN ('open', 'blind')),
    access       TEXT NOT NULL CHECK (access IN ('link', 'personal')),  -- общий QR или одноразовые приглашения
    seats        INTEGER,                                                -- лимит участников, NULL — без лимита
    status       TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft', 'voting', 'closed', 'published')),
    join_code    TEXT NOT NULL UNIQUE,   -- в QR для гостей
    public_code  TEXT NOT NULL UNIQUE,   -- в ссылке на опубликованные итоги
    created_at   TEXT NOT NULL,
    opened_at    TEXT,
    closed_at    TEXT,
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS event_wines (
    id          INTEGER PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES events ON DELETE CASCADE,
    wine_slug   TEXT NOT NULL,           -- ключ карточки в каталоге «Своё Вино»
    position    INTEGER NOT NULL,        -- порядок в программе, он же номер образца вслепую
    flight      TEXT,                    -- название сета
    comment     TEXT,                    -- комментарий ведущего
    revealed_at TEXT,                    -- когда бутылку раскрыли после слепой дегустации
    UNIQUE (event_id, wine_slug)
);

CREATE TABLE IF NOT EXISTS criteria (
    id        INTEGER PRIMARY KEY,
    event_id  INTEGER NOT NULL REFERENCES events ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    name      TEXT NOT NULL,
    hint      TEXT,
    min_value INTEGER NOT NULL,
    max_value INTEGER NOT NULL,
    weight    REAL NOT NULL DEFAULT 1,
    CHECK (max_value > min_value AND weight > 0)
);

CREATE TABLE IF NOT EXISTS participants (
    id              INTEGER PRIMARY KEY,
    event_id        INTEGER NOT NULL REFERENCES events ON DELETE CASCADE,
    token_hash      TEXT NOT NULL UNIQUE,  -- сам токен лежит только в cookie гостя
    name            TEXT NOT NULL,
    adult_confirmed INTEGER NOT NULL,
    device_hash     TEXT,                  -- хеши, не сами данные устройства
    ip_hash         TEXT,
    duplicate_of    INTEGER REFERENCES participants,  -- повторный вход с того же устройства
    excluded        INTEGER NOT NULL DEFAULT 0,       -- исключён организатором
    joined_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    id             INTEGER PRIMARY KEY,
    event_id       INTEGER NOT NULL REFERENCES events ON DELETE CASCADE,
    code           TEXT NOT NULL UNIQUE,
    label          TEXT,
    participant_id INTEGER REFERENCES participants ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS ratings (
    id             INTEGER PRIMARY KEY,
    participant_id INTEGER NOT NULL REFERENCES participants ON DELETE CASCADE,
    event_wine_id  INTEGER NOT NULL REFERENCES event_wines ON DELETE CASCADE,
    note           TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (participant_id, event_wine_id)   -- один участник — одна оценка одного вина
);

CREATE TABLE IF NOT EXISTS rating_values (
    rating_id    INTEGER NOT NULL REFERENCES ratings ON DELETE CASCADE,
    criterion_id INTEGER NOT NULL REFERENCES criteria ON DELETE CASCADE,
    value        INTEGER NOT NULL,
    PRIMARY KEY (rating_id, criterion_id)
);

-- журнал передач в Народный винный рейтинг
CREATE TABLE IF NOT EXISTS transfers (
    id             INTEGER PRIMARY KEY,
    event_id       INTEGER NOT NULL REFERENCES events ON DELETE CASCADE,
    created_at     TEXT NOT NULL,
    votes_sent     INTEGER NOT NULL,
    accepted       INTEGER NOT NULL,
    duplicates     INTEGER NOT NULL,
    rejected       INTEGER NOT NULL,
    payload_sha256 TEXT NOT NULL
);

-- имитация хранилища на стороне платформы: что приняла заглушка приёмника
CREATE TABLE IF NOT EXISTS received_votes (
    vote_id      TEXT PRIMARY KEY,
    wine_slug    TEXT NOT NULL,
    score        REAL NOT NULL,
    source_event TEXT NOT NULL,
    received_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS event_wines_event ON event_wines (event_id, position);
CREATE INDEX IF NOT EXISTS participants_event ON participants (event_id);
CREATE INDEX IF NOT EXISTS ratings_wine ON ratings (event_wine_id);
"""


def app_db_path() -> Path:
    return Path(os.environ.get("APP_DB") or DATA / "app.sqlite")


def catalog_db_path() -> Path:
    return Path(os.environ.get("CATALOG_DB") or DATA / "catalog.sqlite")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    path = app_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # соединение живёт один запрос, но зависимость и обработчик FastAPI могут выполняться в разных потоках
    db = sqlite3.connect(path.resolve().as_uri(), uri=True, timeout=10, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("ATTACH DATABASE ? AS cat", (catalog_db_path().resolve().as_uri() + "?mode=ro",))
    # LIKE в SQLite не знает регистра кириллицы, поэтому ищем по своей lower()
    db.create_function("lower_u", 1, lambda s: s.lower().replace("ё", "е") if s else s, deterministic=True)
    return db


def init_db() -> None:
    db = connect()
    db.executescript(SCHEMA)
    db.commit()
    db.close()


def get_db():
    """Зависимость FastAPI: одно соединение на запрос, фиксация при успехе."""
    db = connect()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
