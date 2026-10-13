"""Анкета участника: вход по QR-коду, оценка вин со смартфона, раскрытие бутылок."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse

from . import scoring
from .db import get_db, now
from .security import new_token, sha256
from .web import client_ip, flash, redirect, render

router = APIRouter()

COOKIE_DAYS = 7


def event_by_code(db, code: str):
    event = db.execute("SELECT * FROM events WHERE join_code = ?", (code,)).fetchone()
    if not event:
        raise HTTPException(status_code=404, detail="Дегустация не найдена — проверьте QR-код")
    return event


def cookie_name(event) -> str:
    return f"tp_{event['id']}"


def current_participant(request: Request, db, event):
    token = request.cookies.get(cookie_name(event))
    if not token:
        return None
    return db.execute(
        "SELECT * FROM participants WHERE token_hash = ? AND event_id = ?", (sha256(token), event["id"])
    ).fetchone()


def identity_visible(event, wine) -> bool:
    """В слепой дегустации гость видит вино только после раскрытия бутылки."""
    return event["mode"] == "open" or wine["revealed_at"] is not None or event["status"] == "published"


def state_version(db, event) -> str:
    revealed = db.execute(
        "SELECT count(*) FROM event_wines WHERE event_id = ? AND revealed_at IS NOT NULL", (event["id"],)
    ).fetchone()[0]
    return f"{event['status']}:{revealed}"


# ---------- вход ----------

@router.get("/j/{code}")
def join_page(request: Request, code: str, db=Depends(get_db)):
    event = event_by_code(db, code)
    if current_participant(request, db, event):
        return redirect(f"/t/{code}")
    return render(request, "guest/join.html", event=event, invite=request.query_params.get("i", ""), name="")


@router.post("/j/{code}")
def join(
    request: Request, code: str, db=Depends(get_db),
    name: str = Form(""), adult: str = Form(""), fp: str = Form(""), invite: str = Form(""),
):
    event = event_by_code(db, code)
    if current_participant(request, db, event):
        return redirect(f"/t/{code}")

    def refuse(text: str):
        flash(request, text, "error")
        return render(request, "guest/join.html", status_code=422, event=event, invite=invite, name=name)

    name = " ".join(name.split())[:40]
    if event["status"] not in ("draft", "voting"):
        return refuse("Голосование уже завершено, присоединиться нельзя")
    if not name:
        return refuse("Представьтесь — имя увидит только организатор")
    if adult != "yes":
        return refuse("Участвовать в дегустации могут только совершеннолетние")
    active = db.execute(
        "SELECT count(*) FROM participants WHERE event_id = ? AND excluded = 0", (event["id"],)
    ).fetchone()[0]
    if event["seats"] and active >= event["seats"]:
        return refuse("Все места заняты — обратитесь к организатору")

    invite_row = None
    if event["access"] == "personal":
        invite_row = db.execute(
            "SELECT * FROM invites WHERE code = ? AND event_id = ?", (invite.strip().lower(), event["id"])
        ).fetchone()
        if not invite_row:
            return refuse("Нужен код из личного приглашения — он указан под вашим QR-кодом")
        if invite_row["participant_id"]:
            return refuse("Это приглашение уже использовано. Попросите у организатора новое")

    # хеши привязаны к событию: по ним нельзя узнать устройство и нельзя связать гостя между дегустациями
    device_hash = sha256(f"{event['id']}|{fp[:500]}|{request.headers.get('user-agent', '')}")[:32]
    ip_hash = sha256(f"{event['id']}|{client_ip(request)}")[:32]
    duplicate_of = None
    if event["access"] == "link":
        # одноразовое приглашение само исключает повтор, поэтому устройство сверяем только при общем QR
        earlier = db.execute(
            "SELECT id FROM participants WHERE event_id = ? AND device_hash = ? AND ip_hash = ? ORDER BY id LIMIT 1",
            (event["id"], device_hash, ip_hash),
        ).fetchone()
        duplicate_of = earlier["id"] if earlier else None

    token = new_token()
    cursor = db.execute(
        """INSERT INTO participants (event_id, token_hash, name, adult_confirmed, device_hash, ip_hash,
                                     duplicate_of, joined_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
        (event["id"], sha256(token), name, device_hash, ip_hash, duplicate_of, now()),
    )
    if invite_row:
        claimed = db.execute(
            "UPDATE invites SET participant_id = ? WHERE id = ? AND participant_id IS NULL",
            (cursor.lastrowid, invite_row["id"]),
        )
        if claimed.rowcount == 0:  # приглашение успели занять между проверкой и записью
            raise HTTPException(status_code=409, detail="Это приглашение уже использовано")
    response = redirect(f"/t/{code}")
    response.set_cookie(cookie_name(event), token, max_age=COOKIE_DAYS * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
    return response


# ---------- дегустация ----------

def guest_view(db, event, person) -> dict:
    """Что показать гостю: его оценки и — где уже можно — общие результаты."""
    data = scoring.tally(db, event["id"])
    mine = {b["event_wine_id"]: b for b in data["ballots"] if b["participant_id"] == person["id"]}
    group = {r["wine"]["id"]: r for r in scoring.summarize(data)}
    items = []
    for wine in data["wines"]:
        visible = identity_visible(event, wine)
        # общий балл: в открытой — после публикации, в слепой — вместе с раскрытием бутылки
        show_group = event["status"] == "published" or (event["mode"] == "blind" and wine["revealed_at"])
        items.append({
            "wine": wine, "visible": visible, "ballot": mine.get(wine["id"]),
            "group": group[wine["id"]] if show_group else None,
        })
    rated = sum(1 for i in items if i["ballot"] and i["ballot"]["score"] is not None)
    return {"items": items, "criteria": data["criteria"], "rated": rated, "total": len(items)}


def event_and_person(request: Request, db, code: str):
    event = event_by_code(db, code)
    person = current_participant(request, db, event)
    return event, person


@router.get("/t/{code}")
def tasting(request: Request, code: str, db=Depends(get_db)):
    event, person = event_and_person(request, db, code)
    if not person:
        return redirect(f"/j/{code}")
    return render(request, "guest/tasting.html", event=event, person=person,
                  version=state_version(db, event), **guest_view(db, event, person))


@router.get("/t/{code}/state")
def state(code: str, db=Depends(get_db)):
    return JSONResponse({"v": state_version(db, event_by_code(db, code))})


def find_item(view: dict, event_wine_id: int):
    for index, item in enumerate(view["items"]):
        if item["wine"]["id"] == event_wine_id:
            return index, item
    raise HTTPException(status_code=404, detail="Образец не найден")


@router.get("/t/{code}/w/{event_wine_id}")
def rate_page(request: Request, code: str, event_wine_id: int, db=Depends(get_db)):
    event, person = event_and_person(request, db, code)
    if not person:
        return redirect(f"/j/{code}")
    view = guest_view(db, event, person)
    index, item = find_item(view, event_wine_id)
    return render(request, "guest/rate.html", event=event, person=person, item=item, index=index,
                  criteria=view["criteria"], total=view["total"], version=state_version(db, event))


@router.post("/t/{code}/w/{event_wine_id}")
async def rate(request: Request, code: str, event_wine_id: int, db=Depends(get_db)):
    event, person = event_and_person(request, db, code)
    if not person:
        return redirect(f"/j/{code}")
    if event["status"] != "voting":
        flash(request, "Голосование сейчас закрыто — оценка не сохранена", "error")
        return redirect(f"/t/{code}")
    view = guest_view(db, event, person)
    find_item(view, event_wine_id)

    form = await request.form()
    values = {}
    for criterion in view["criteria"]:
        raw = str(form.get(f"c_{criterion['id']}") or "")
        if not raw.lstrip("-").isdigit() or not criterion["min_value"] <= int(raw) <= criterion["max_value"]:
            flash(request, f"Поставьте оценку по критерию «{criterion['name']}»", "error")
            return redirect(f"/t/{code}/w/{event_wine_id}")
        values[criterion["id"]] = int(raw)
    note = str(form.get("note") or "").strip()[:500] or None

    stamp = now()
    # UNIQUE (participant_id, event_wine_id): повторная отправка обновляет оценку, а не добавляет вторую
    db.execute(
        """INSERT INTO ratings (participant_id, event_wine_id, note, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (participant_id, event_wine_id) DO UPDATE SET note = excluded.note,
                                                                     updated_at = excluded.updated_at""",
        (person["id"], event_wine_id, note, stamp, stamp),
    )
    rating_id = db.execute(
        "SELECT id FROM ratings WHERE participant_id = ? AND event_wine_id = ?", (person["id"], event_wine_id)
    ).fetchone()["id"]
    db.execute("DELETE FROM rating_values WHERE rating_id = ?", (rating_id,))
    db.executemany("INSERT INTO rating_values VALUES (?, ?, ?)", [(rating_id, c, v) for c, v in values.items()])

    # ведём к следующему неоценённому образцу, а когда всё оценено — к списку
    order = [i["wine"]["id"] for i in view["items"]]
    done = {i["wine"]["id"] for i in view["items"] if i["ballot"] and i["ballot"]["score"] is not None}
    done.add(event_wine_id)
    start = order.index(event_wine_id)
    for wine_id in order[start + 1:] + order[:start]:
        if wine_id not in done:
            return redirect(f"/t/{code}/w/{wine_id}")
    flash(request, "Все вина оценены. Оценки можно менять, пока голосование открыто")
    return redirect(f"/t/{code}")
