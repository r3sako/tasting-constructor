"""Передача валидных оценок в Народный винный рейтинг.

Публичного API у рейтинга нет, поэтому здесь описан контракт и собрана заглушка
приёмника: она проверяет подпись и содержимое так, как это делала бы платформа.
Если задан RATING_API_URL, выгрузка уходит туда тем же запросом.
"""

import json
import os
import re

import httpx

from . import scoring
from .db import now
from .security import sha256, sign

SCHEMA_NAME = "svoe-vino.tasting-votes/1"
CATALOG_URL = "https://vino-svoe.ru/wines/"


def build_payload(db, event) -> dict:
    data = scoring.tally(db, event["id"])
    slug_by_wine = {w["id"]: w["wine_slug"] for w in data["wines"]}
    organizer = db.execute("SELECT name FROM organizers WHERE id = ?", (event["organizer_id"],)).fetchone()
    votes = []
    for b in data["ballots"]:
        if b["reason"] is not None:
            continue
        slug = slug_by_wine[b["event_wine_id"]]
        votes.append({
            # идентификатор детерминированный: повторная отправка той же оценки не создаст второй голос
            "vote_id": sha256(f"{event['public_code']}:{b['participant_id']}:{slug}")[:32],
            "wine_slug": slug,
            "wine_url": CATALOG_URL + slug,
            "score": b["score"],
            "voter": sha256(f"{event['public_code']}:{b['participant_id']}:voter")[:16],  # псевдоним, не имя
            "rated_at": b["updated_at"],
        })
    votes.sort(key=lambda v: v["vote_id"])
    counts = scoring.validity_counts(data)
    return {
        "schema": SCHEMA_NAME,
        "source": {
            "system": "tasting-constructor",
            "event_id": event["public_code"],
            "title": event["title"],
            "organizer": organizer["name"],
            "tasting_mode": event["mode"],
            "access": event["access"],
            "held_at": event["starts_at"],
            "closed_at": event["closed_at"],
        },
        "scale": {
            "target": {"min": scoring.TARGET_MIN, "max": scoring.TARGET_MAX},
            "method": "weighted mean of min-max normalized criteria",
            "criteria": [
                {"name": c["name"], "min": c["min_value"], "max": c["max_value"], "weight": c["weight"]}
                for c in data["criteria"]
            ],
        },
        "votes": votes,
        "summary": {
            "participants": len(data["participants"]),
            "valid_votes": counts["valid"],
            "excluded_votes": {reason: counts[reason] for reason in scoring.REASONS},
        },
    }


def encode(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def receive(db, body: bytes, signature: str) -> tuple[int, dict]:
    """Заглушка приёмника на стороне платформы. Возвращает HTTP-код и ответ."""
    if not signature or signature.removeprefix("sha256=") != sign(body):
        return 401, {"error": "bad signature"}
    try:
        payload = json.loads(body)
    except ValueError:
        return 400, {"error": "invalid json"}
    if payload.get("schema") != SCHEMA_NAME or not isinstance(payload.get("votes"), list):
        return 400, {"error": "unsupported schema"}
    source = str((payload.get("source") or {}).get("event_id") or "")
    accepted = duplicates = 0
    rejected = []
    for vote in payload["votes"]:
        vote_id, slug, score = vote.get("vote_id"), vote.get("wine_slug"), vote.get("score")
        if not (isinstance(vote_id, str) and re.fullmatch(r"[0-9a-f]{32}", vote_id)):
            rejected.append({"vote_id": vote_id, "reason": "bad vote_id"})
        elif not isinstance(score, (int, float)) or not scoring.TARGET_MIN <= score <= scoring.TARGET_MAX:
            rejected.append({"vote_id": vote_id, "reason": "score out of range"})
        elif not db.execute("SELECT 1 FROM cat.wines WHERE slug = ?", (slug,)).fetchone():
            rejected.append({"vote_id": vote_id, "reason": "unknown wine"})
        elif db.execute("SELECT 1 FROM received_votes WHERE vote_id = ?", (vote_id,)).fetchone():
            duplicates += 1
        else:
            db.execute("INSERT INTO received_votes VALUES (?, ?, ?, ?, ?)", (vote_id, slug, score, source, now()))
            accepted += 1
    return 200, {"accepted": accepted, "duplicates": duplicates, "rejected": rejected}


def transfer(db, event) -> dict:
    """Отправляет выгрузку и записывает результат в журнал передач."""
    payload = build_payload(db, event)
    body = encode(payload)
    signature = "sha256=" + sign(body)
    url = os.environ.get("RATING_API_URL")
    if url:
        response = httpx.post(
            url, content=body, timeout=20,
            headers={"Content-Type": "application/json", "X-Signature": signature},
        )
        status, answer = response.status_code, response.json()
    else:
        status, answer = receive(db, body, signature)
    if status != 200:
        raise RuntimeError(f"приёмник ответил {status}: {answer}")
    db.execute(
        "INSERT INTO transfers (event_id, created_at, votes_sent, accepted, duplicates, rejected, payload_sha256)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event["id"], now(), len(payload["votes"]), answer["accepted"], answer["duplicates"],
         len(answer["rejected"]), sha256(body.decode())),
    )
    return answer
