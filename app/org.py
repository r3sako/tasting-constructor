"""Кабинет организатора: событие, вина и программа, шкалы, приглашения, участники, итоги."""

import csv
import io
import json
import random

import segno
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from . import export, scoring
from .db import get_db, now
from .security import new_code
from .web import flash, organizer, owned_event, public_url, redirect, render

router = APIRouter(prefix="/org")

MAX_WINES = 30
PAGE_SIZE = 24
FILTERS = ("q", "color", "sweetness", "region", "grape", "rated")


def qr_svg(url: str, scale: int = 6) -> str:
    return segno.make(url, error="m").svg_inline(scale=scale, dark="#3b0f1d", light="#ffffff", border=2)


def event_context(db, event) -> dict:
    """Счётчики для шапки события и вкладок."""
    one = lambda sql: db.execute(sql, (event["id"],)).fetchone()[0]
    return {
        "event": event,
        "base": f"/org/events/{event['id']}",
        "n_wines": one("SELECT count(*) FROM event_wines WHERE event_id = ?"),
        "n_criteria": one("SELECT count(*) FROM criteria WHERE event_id = ?"),
        "n_people": one("SELECT count(*) FROM participants WHERE event_id = ?"),
        "n_review": one(
            "SELECT count(*) FROM participants WHERE event_id = ? AND duplicate_of IS NOT NULL AND excluded = 0"
        ),
        "n_ratings": one(
            "SELECT count(*) FROM ratings r JOIN participants p ON p.id = r.participant_id WHERE p.event_id = ?"
        ),
    }


def require_draft(event) -> None:
    if event["status"] != "draft":
        raise HTTPException(status_code=409, detail="Состав и шкалы меняются только в черновике")


def set_preset(db, event_id: int, key: str) -> None:
    db.execute("DELETE FROM criteria WHERE event_id = ?", (event_id,))
    for position, (name, hint, low, high, weight) in enumerate(scoring.PRESETS[key]["criteria"], 1):
        db.execute(
            "INSERT INTO criteria (event_id, position, name, hint, min_value, max_value, weight)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, position, name, hint, low, high, weight),
        )


def renumber(db, event_id: int) -> None:
    rows = db.execute("SELECT id FROM event_wines WHERE event_id = ? ORDER BY position, id", (event_id,))
    for position, row in enumerate(rows.fetchall(), 1):
        db.execute("UPDATE event_wines SET position = ? WHERE id = ?", (position, row["id"]))


# ---------- список и создание ----------

@router.get("")
def events_list(request: Request, org=Depends(organizer), db=Depends(get_db)):
    events = db.execute(
        """SELECT e.*,
                  (SELECT count(*) FROM event_wines WHERE event_id = e.id) AS n_wines,
                  (SELECT count(*) FROM participants WHERE event_id = e.id) AS n_people
           FROM events e WHERE organizer_id = ? ORDER BY e.created_at DESC""",
        (org["id"],),
    ).fetchall()
    return render(request, "org/events.html", org=org, events=events)


def read_event_form(title, description, place, starts_at, mode, access, seats) -> dict:
    title = title.strip()
    if not 2 <= len(title) <= 120:
        raise ValueError("Название — от 2 до 120 символов")
    if mode not in ("open", "blind") or access not in ("link", "personal"):
        raise ValueError("Неизвестный формат дегустации")
    seats_value = None
    if seats.strip():
        if not seats.strip().isdigit() or not 1 <= int(seats) <= 500:
            raise ValueError("Число мест — от 1 до 500 или пусто")
        seats_value = int(seats)
    return {
        "title": title, "description": description.strip() or None, "place": place.strip() or None,
        "starts_at": starts_at or None, "mode": mode, "access": access, "seats": seats_value,
    }


@router.get("/events/new")
def event_new(request: Request, org=Depends(organizer)):
    return render(request, "org/event_form.html", org=org, event=None, form={})


@router.post("/events/new")
def event_create(
    request: Request, org=Depends(organizer), db=Depends(get_db),
    title: str = Form(""), description: str = Form(""), place: str = Form(""), starts_at: str = Form(""),
    mode: str = Form("open"), access: str = Form("link"), seats: str = Form(""),
):
    raw = dict(title=title, description=description, place=place, starts_at=starts_at, mode=mode,
               access=access, seats=seats)
    try:
        data = read_event_form(**raw)
    except ValueError as e:
        flash(request, str(e), "error")
        return render(request, "org/event_form.html", status_code=422, org=org, event=None, form=raw)
    cursor = db.execute(
        """INSERT INTO events (organizer_id, title, description, place, starts_at, mode, access, seats,
                               join_code, public_code, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (org["id"], data["title"], data["description"], data["place"], data["starts_at"], data["mode"],
         data["access"], data["seats"], new_code(8), new_code(10), now()),
    )
    set_preset(db, cursor.lastrowid, "stars")
    flash(request, "Дегустация создана. Теперь подберите вина.")
    return redirect(f"/org/events/{cursor.lastrowid}/wines")


@router.get("/events/{event_id}/edit")
def event_edit(request: Request, org=Depends(organizer), event=Depends(owned_event)):
    return render(request, "org/event_form.html", org=org, event=event, form=dict(event))


@router.post("/events/{event_id}/edit")
def event_update(
    request: Request, org=Depends(organizer), event=Depends(owned_event), db=Depends(get_db),
    title: str = Form(""), description: str = Form(""), place: str = Form(""), starts_at: str = Form(""),
    mode: str = Form(""), access: str = Form(""), seats: str = Form(""),
):
    # формат и способ входа после старта не меняются: от них зависит, что уже видели гости
    locked = event["status"] != "draft"
    raw = dict(title=title, description=description, place=place, starts_at=starts_at,
               mode=event["mode"] if locked else mode, access=event["access"] if locked else access, seats=seats)
    try:
        data = read_event_form(**raw)
    except ValueError as e:
        flash(request, str(e), "error")
        return render(request, "org/event_form.html", status_code=422, org=org, event=event, form=raw)
    db.execute(
        "UPDATE events SET title = ?, description = ?, place = ?, starts_at = ?, mode = ?, access = ?, seats = ?"
        " WHERE id = ?",
        (data["title"], data["description"], data["place"], data["starts_at"], data["mode"], data["access"],
         data["seats"], event["id"]),
    )
    flash(request, "Изменения сохранены")
    return redirect(f"/org/events/{event['id']}")


@router.post("/events/{event_id}/delete")
def event_delete(request: Request, event=Depends(owned_event), db=Depends(get_db)):
    db.execute("DELETE FROM events WHERE id = ?", (event["id"],))
    flash(request, f"Дегустация «{event['title']}» удалена")
    return redirect("/org")


# ---------- обзор и статусы ----------

@router.get("/events/{event_id}")
def overview(request: Request, org=Depends(organizer), event=Depends(owned_event), db=Depends(get_db)):
    ctx = event_context(db, event)
    unrevealed = db.execute(
        "SELECT count(*) FROM event_wines WHERE event_id = ? AND revealed_at IS NULL", (event["id"],)
    ).fetchone()[0]
    return render(request, "org/overview.html", org=org, tab="overview", unrevealed=unrevealed,
                  join_url=f"{public_url(request)}/j/{event['join_code']}",
                  results_url=f"{public_url(request)}/r/{event['public_code']}", **ctx)


@router.post("/events/{event_id}/status")
def change_status(request: Request, event=Depends(owned_event), db=Depends(get_db), action: str = Form(...)):
    ctx = event_context(db, event)
    status, eid = event["status"], event["id"]
    back = redirect(f"/org/events/{eid}")
    if action == "start" and status == "draft":
        if ctx["n_wines"] < 1 or ctx["n_criteria"] < 1:
            flash(request, "Добавьте хотя бы одно вино и одну шкалу", "error")
            return back
        db.execute("UPDATE events SET status = 'voting', opened_at = ? WHERE id = ?", (now(), eid))
        flash(request, "Голосование открыто — показывайте гостям QR-код")
    elif action == "close" and status == "voting":
        db.execute("UPDATE events SET status = 'closed', closed_at = ? WHERE id = ?", (now(), eid))
        flash(request, "Голосование закрыто, оценки больше не меняются")
    elif action == "reopen" and status == "closed":
        revealed = db.execute(
            "SELECT count(*) FROM event_wines WHERE event_id = ? AND revealed_at IS NOT NULL", (eid,)
        ).fetchone()[0]
        transferred = db.execute("SELECT count(*) FROM transfers WHERE event_id = ?", (eid,)).fetchone()[0]
        if revealed or transferred:
            flash(request, "Нельзя возобновить: бутылки уже раскрыты или оценки переданы в рейтинг", "error")
            return back
        db.execute("UPDATE events SET status = 'voting', closed_at = NULL WHERE id = ?", (eid,))
        flash(request, "Голосование возобновлено")
    elif action == "publish" and status == "closed":
        db.execute("UPDATE event_wines SET revealed_at = ? WHERE event_id = ? AND revealed_at IS NULL", (now(), eid))
        db.execute("UPDATE events SET status = 'published', published_at = ? WHERE id = ?", (now(), eid))
        flash(request, "Итоги опубликованы")
    elif action == "unpublish" and status == "published":
        db.execute("UPDATE events SET status = 'closed', published_at = NULL WHERE id = ?", (eid,))
        flash(request, "Итоги сняты с публикации")
    elif action == "to_draft" and status == "voting":
        if ctx["n_ratings"]:
            flash(request, "Уже есть оценки — вернуть в черновик нельзя", "error")
            return back
        db.execute("UPDATE events SET status = 'draft', opened_at = NULL WHERE id = ?", (eid,))
        flash(request, "Дегустация возвращена в черновик")
    else:
        flash(request, "Это действие сейчас недоступно", "error")
    return back


# ---------- вина и программа ----------

def catalog_search(db, params: dict, limit: int, offset: int = 0):
    where, args = ["1 = 1"], []
    if params.get("q"):
        for word in params["q"].lower().replace("ё", "е").split()[:5]:
            where.append("(lower_u(w.title) LIKE ? OR lower_u(w.manufacturer) LIKE ?)")
            args += [f"%{word}%"] * 2
    for field in ("color", "sweetness", "region"):
        if params.get(field):
            where.append(f"w.{field} = ?")
            args.append(params[field])
    if params.get("grape"):
        where.append("EXISTS (SELECT 1 FROM cat.wine_grapes g WHERE g.slug = w.slug AND g.grape = ?)")
        args.append(params["grape"])
    if params.get("rated"):
        where.append("w.public_rating IS NOT NULL")
    clause = " AND ".join(where)
    total = db.execute(f"SELECT count(*) FROM cat.wines w WHERE {clause}", args).fetchone()[0]
    rows = db.execute(
        f"""SELECT w.*, (SELECT group_concat(grape, ', ') FROM cat.wine_grapes g WHERE g.slug = w.slug) AS grapes
            FROM cat.wines w WHERE {clause}
            ORDER BY w.public_rating IS NULL, w.public_rating DESC, w.title LIMIT ? OFFSET ?""",
        [*args, limit, offset],
    ).fetchall()
    return rows, total


def filter_options(db) -> dict:
    column = lambda sql: [r[0] for r in db.execute(sql)]
    return {
        "colors": column("SELECT DISTINCT color FROM cat.wines WHERE color IS NOT NULL ORDER BY 1"),
        "sweetness": column("SELECT sweetness FROM cat.wines GROUP BY 1 ORDER BY count(*) DESC"),
        "regions": column("SELECT region FROM cat.wines WHERE region IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC"),
        "grapes": column("SELECT grape FROM cat.wine_grapes GROUP BY 1 ORDER BY count(*) DESC"),
    }


def program_rows(db, event_id: int):
    return db.execute(
        """SELECT ew.*, w.title, w.manufacturer, w.region, w.category, w.image, w.public_rating
           FROM event_wines ew LEFT JOIN cat.wines w ON w.slug = ew.wine_slug
           WHERE ew.event_id = ? ORDER BY ew.position, ew.id""",
        (event_id,),
    ).fetchall()


@router.get("/events/{event_id}/wines")
def wines_page(request: Request, org=Depends(organizer), event=Depends(owned_event), db=Depends(get_db)):
    params = {k: request.query_params.get(k, "").strip() for k in FILTERS}
    raw_page = request.query_params.get("page", "1")
    page = int(raw_page) if raw_page.isdigit() and int(raw_page) > 0 else 1
    found, total = catalog_search(db, params, PAGE_SIZE, (page - 1) * PAGE_SIZE)
    program = program_rows(db, event["id"])
    return render(
        request, "org/wines.html", org=org, tab="wines", program=program, found=found, total=total,
        page=page, pages=max(1, -(-total // PAGE_SIZE)), params=params, options=filter_options(db),
        chosen={w["wine_slug"] for w in program}, query=request.url.query, max_wines=MAX_WINES,
        **event_context(db, event),
    )


def add_wine(db, event_id: int, slug: str) -> bool:
    if not db.execute("SELECT 1 FROM cat.wines WHERE slug = ?", (slug,)).fetchone():
        return False  # в программу попадают только вина из каталога
    count, last = db.execute(
        "SELECT count(*), coalesce(max(position), 0) FROM event_wines WHERE event_id = ?", (event_id,)
    ).fetchone()
    if count >= MAX_WINES:
        return False
    cursor = db.execute(
        "INSERT OR IGNORE INTO event_wines (event_id, wine_slug, position) VALUES (?, ?, ?)",
        (event_id, slug, last + 1),
    )
    return cursor.rowcount > 0


@router.post("/events/{event_id}/wines/add")
def wines_add(request: Request, event=Depends(owned_event), db=Depends(get_db),
              slug: str = Form(...), query: str = Form("")):
    require_draft(event)
    if not add_wine(db, event["id"], slug):
        flash(request, f"Не удалось добавить: вино уже в программе или достигнут лимит в {MAX_WINES} вин", "error")
    return redirect(f"/org/events/{event['id']}/wines?{query}#catalog")


@router.post("/events/{event_id}/wines/add-top")
def wines_add_top(request: Request, event=Depends(owned_event), db=Depends(get_db),
                  query: str = Form(""), count: int = Form(6)):
    """Быстрый подбор: лучшие по Народному рейтингу среди найденных по текущему фильтру."""
    require_draft(event)
    from urllib.parse import parse_qs
    parsed = parse_qs(query)
    params = {k: (parsed.get(k) or [""])[0].strip() for k in FILTERS}
    params["rated"] = "1"
    found, _ = catalog_search(db, params, 60)
    added = 0
    for wine in found:
        if added >= max(1, min(count, 12)):
            break
        added += add_wine(db, event["id"], wine["slug"])
    flash(request, f"Добавлено вин: {added}" if added else "Подходящих вин с рейтингом не нашлось",
          "ok" if added else "error")
    return redirect(f"/org/events/{event['id']}/wines?{query}")


@router.post("/events/{event_id}/program")
async def program_save(request: Request, event=Depends(owned_event), db=Depends(get_db)):
    """Сохраняет сеты и комментарии; кнопки в строках двигают, убирают и перемешивают вина."""
    form = await request.form()
    rows = program_rows(db, event["id"])
    for row in rows:
        db.execute(
            "UPDATE event_wines SET flight = ?, comment = ? WHERE id = ?",
            ((form.get(f"flight_{row['id']}") or "").strip()[:60] or None,
             (form.get(f"comment_{row['id']}") or "").strip()[:300] or None, row["id"]),
        )
    action = form.get("action") or "save"
    if action != "save":
        require_draft(event)
    ids = [r["id"] for r in rows]
    if action == "shuffle":
        random.shuffle(ids)
    elif ":" in action:
        verb, _, raw_id = action.partition(":")
        target = int(raw_id) if raw_id.isdigit() else None
        if target in ids:
            i = ids.index(target)
            if verb == "up" and i > 0:
                ids[i - 1], ids[i] = ids[i], ids[i - 1]
            elif verb == "down" and i < len(ids) - 1:
                ids[i + 1], ids[i] = ids[i], ids[i + 1]
            elif verb == "remove":
                ids.remove(target)
                db.execute("DELETE FROM event_wines WHERE id = ?", (target,))
    for position, ew_id in enumerate(ids, 1):
        db.execute("UPDATE event_wines SET position = ? WHERE id = ?", (position, ew_id))
    if action in ("save", "shuffle"):
        flash(request, "Порядок перемешан" if action == "shuffle" else "Программа сохранена")
    return redirect(f"/org/events/{event['id']}/wines")


# ---------- шкалы ----------

@router.get("/events/{event_id}/scales")
def scales_page(request: Request, org=Depends(organizer), event=Depends(owned_event), db=Depends(get_db)):
    criteria = db.execute("SELECT * FROM criteria WHERE event_id = ? ORDER BY position, id", (event["id"],)).fetchall()
    return render(request, "org/scales.html", org=org, tab="scales", criteria=criteria,
                  presets=scoring.PRESETS, **event_context(db, event))


@router.post("/events/{event_id}/scales/preset")
def scales_preset(request: Request, event=Depends(owned_event), db=Depends(get_db), key: str = Form(...)):
    require_draft(event)
    if key not in scoring.PRESETS:
        raise HTTPException(status_code=400)
    set_preset(db, event["id"], key)
    flash(request, f"Применён шаблон «{scoring.PRESETS[key]['title']}»")
    return redirect(f"/org/events/{event['id']}/scales")


@router.post("/events/{event_id}/scales")
async def scales_save(request: Request, event=Depends(owned_event), db=Depends(get_db)):
    require_draft(event)
    form = await request.form()
    names, hints = form.getlist("name"), form.getlist("hint")
    lows, highs, weights = form.getlist("min"), form.getlist("max"), form.getlist("weight")
    criteria = []
    try:
        for name, hint, low, high, weight in zip(names, hints, lows, highs, weights):
            if not name.strip():
                continue  # пустая строка — способ удалить критерий
            low, high, weight = int(low), int(high), float(str(weight).replace(",", "."))
            if not (0 <= low < high <= 100) or not (0 < weight <= 100):
                raise ValueError
            criteria.append((name.strip()[:40], hint.strip()[:120] or None, low, high, weight))
    except ValueError:
        flash(request, "Проверьте шкалы: минимум меньше максимума, значения от 0 до 100, вес больше нуля", "error")
        return redirect(f"/org/events/{event['id']}/scales")
    if not 1 <= len(criteria) <= 8:
        flash(request, "Нужно от одного до восьми критериев", "error")
        return redirect(f"/org/events/{event['id']}/scales")
    db.execute("DELETE FROM criteria WHERE event_id = ?", (event["id"],))
    for position, row in enumerate(criteria, 1):
        db.execute(
            "INSERT INTO criteria (event_id, position, name, hint, min_value, max_value, weight)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event["id"], position, *row),
        )
    flash(request, "Шкалы сохранены")
    return redirect(f"/org/events/{event['id']}/scales")


# ---------- приглашение ----------

@router.get("/events/{event_id}/invite")
def invite_page(request: Request, org=Depends(organizer), event=Depends(owned_event), db=Depends(get_db)):
    join_url = f"{public_url(request)}/j/{event['join_code']}"
    invites = db.execute(
        """SELECT i.*, p.name AS guest FROM invites i LEFT JOIN participants p ON p.id = i.participant_id
           WHERE i.event_id = ? ORDER BY i.id""",
        (event["id"],),
    ).fetchall()
    return render(request, "org/invite.html", org=org, tab="invite", join_url=join_url,
                  qr=qr_svg(join_url, 8), invites=invites, **event_context(db, event))


@router.post("/events/{event_id}/invites")
def invites_generate(request: Request, event=Depends(owned_event), db=Depends(get_db), count: int = Form(10)):
    existing = db.execute("SELECT count(*) FROM invites WHERE event_id = ?", (event["id"],)).fetchone()[0]
    count = max(1, min(count, 200 - existing))
    for n in range(existing + 1, existing + count + 1):
        db.execute("INSERT INTO invites (event_id, code, label) VALUES (?, ?, ?)",
                   (event["id"], new_code(10), f"Гость {n}"))
    flash(request, f"Создано приглашений: {count}")
    return redirect(f"/org/events/{event['id']}/invite")


@router.get("/events/{event_id}/invites/print")
def invites_print(request: Request, event=Depends(owned_event), db=Depends(get_db)):
    base = f"{public_url(request)}/j/{event['join_code']}?i="
    invites = [
        {"label": i["label"], "code": i["code"], "used": i["participant_id"] is not None, "qr": qr_svg(base + i["code"], 4)}
        for i in db.execute("SELECT * FROM invites WHERE event_id = ? ORDER BY id", (event["id"],))
    ]
    return render(request, "org/invites_print.html", event=event, invites=invites)


# ---------- участники ----------

@router.get("/events/{event_id}/participants")
def participants_page(request: Request, org=Depends(organizer), event=Depends(owned_event), db=Depends(get_db)):
    data = scoring.tally(db, event["id"])
    names = {p["id"]: p["name"] for p in data["participants"]}
    locked = db.execute("SELECT 1 FROM transfers WHERE event_id = ?", (event["id"],)).fetchone() is not None
    return render(request, "org/participants.html", org=org, tab="participants", people=data["participants"],
                  names=names, reasons=scoring.REASONS, total_wines=len(data["wines"]), locked=locked,
                  **event_context(db, event))


@router.post("/events/{event_id}/participants/{participant_id}")
def participant_action(request: Request, participant_id: int, event=Depends(owned_event),
                       db=Depends(get_db), action: str = Form(...)):
    person = db.execute(
        "SELECT * FROM participants WHERE id = ? AND event_id = ?", (participant_id, event["id"])
    ).fetchone()
    if not person:
        raise HTTPException(status_code=404)
    if db.execute("SELECT 1 FROM transfers WHERE event_id = ?", (event["id"],)).fetchone():
        flash(request, "Оценки уже переданы в рейтинг — состав валидных участников зафиксирован", "error")
    elif action == "exclude":
        db.execute("UPDATE participants SET excluded = 1 WHERE id = ?", (participant_id,))
        flash(request, f"{person['name']}: оценки исключены из итогов")
    elif action == "restore":
        db.execute("UPDATE participants SET excluded = 0 WHERE id = ?", (participant_id,))
        flash(request, f"{person['name']}: оценки снова учитываются")
    elif action == "confirm":
        db.execute("UPDATE participants SET duplicate_of = NULL WHERE id = ?", (participant_id,))
        flash(request, f"{person['name']}: подтверждён как отдельный гость")
    return redirect(f"/org/events/{event['id']}/participants")


# ---------- итоги, раскрытие, передача ----------

def results_context(db, event) -> dict:
    data = scoring.tally(db, event["id"])
    return {
        "rows": scoring.summarize(data),
        "criteria": data["criteria"],
        "counts": scoring.validity_counts(data),
        "voters": sum(p["valid_ballots"] > 0 for p in data["participants"]),
        "reasons": scoring.REASONS,
    }


@router.get("/events/{event_id}/results")
def results_page(request: Request, org=Depends(organizer), event=Depends(owned_event), db=Depends(get_db)):
    transfers = db.execute("SELECT * FROM transfers WHERE event_id = ? ORDER BY id DESC", (event["id"],)).fetchall()
    return render(request, "org/results.html", org=org, tab="results", transfers=transfers,
                  results_url=f"{public_url(request)}/r/{event['public_code']}",
                  **results_context(db, event), **event_context(db, event))


def reveal_next(db, event) -> None:
    """Раскрывает следующий образец — от худшего к лучшему, чтобы победитель открылся последним."""
    rows = scoring.summarize(scoring.tally(db, event["id"]))
    hidden = [r for r in reversed(rows) if r["wine"]["revealed_at"] is None]
    if hidden:
        db.execute("UPDATE event_wines SET revealed_at = ? WHERE id = ?", (now(), hidden[0]["wine"]["id"]))


@router.post("/events/{event_id}/reveal")
def reveal(request: Request, event=Depends(owned_event), db=Depends(get_db),
           target: str = Form(...), back: str = Form("results")):
    if event["mode"] != "blind" or event["status"] != "closed":
        flash(request, "Раскрывать бутылки можно после закрытия слепой дегустации", "error")
    elif target == "next":
        reveal_next(db, event)
    elif target == "all":
        db.execute("UPDATE event_wines SET revealed_at = ? WHERE event_id = ? AND revealed_at IS NULL",
                   (now(), event["id"]))
    elif target.isdigit():
        db.execute("UPDATE event_wines SET revealed_at = ? WHERE id = ? AND event_id = ? AND revealed_at IS NULL",
                   (now(), int(target), event["id"]))
    return redirect(f"/org/events/{event['id']}/{'screen' if back == 'screen' else 'results'}")


@router.post("/events/{event_id}/transfer")
def transfer(request: Request, event=Depends(owned_event), db=Depends(get_db)):
    if event["status"] not in ("closed", "published"):
        flash(request, "Передать оценки можно после закрытия голосования", "error")
    else:
        try:
            answer = export.transfer(db, event)
            flash(request, f"Передано в Народный рейтинг: принято {answer['accepted']}, "
                           f"уже было {answer['duplicates']}, отклонено {len(answer['rejected'])}")
        except Exception as e:  # сеть или отказ приёмника — показываем организатору, а не падаем
            flash(request, f"Передача не удалась: {e}", "error")
    return redirect(f"/org/events/{event['id']}/results")


@router.get("/events/{event_id}/export.json")
def export_json(event=Depends(owned_event), db=Depends(get_db)):
    body = json.dumps(export.build_payload(db, event), ensure_ascii=False, indent=2)
    return Response(body, media_type="application/json; charset=utf-8")


@router.get("/events/{event_id}/export.csv")
def export_csv(event=Depends(owned_event), db=Depends(get_db)):
    ctx = results_context(db, event)
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["Место", "Образец", "Вино", "Производитель", "Регион", "Оценка 1–5", "Голосов", "Разброс",
                     *[c["name"] for c in ctx["criteria"]], "Народный рейтинг платформы", "Карточка"])
    for r in ctx["rows"]:
        w = r["wine"]
        writer.writerow([r["place"] or "", w["position"], w["title"], w["manufacturer"], w["region"],
                         r["score"] if r["score"] is not None else "", r["votes"], r["spread"] or "",
                         *[r["criteria"][c["id"]] or "" for c in ctx["criteria"]],
                         w["public_rating"] or "", w["url"]])
    # BOM — чтобы Excel открыл кириллицу без настройки кодировки
    return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="tasting-{event["public_code"]}.csv"'})


# ---------- экран для проектора ----------

def live_version(db, event) -> str:
    """Меняется при любом событии, которое должно обновить экран."""
    row = db.execute(
        """SELECT (SELECT status FROM events WHERE id = :e),
                  (SELECT count(*) FROM participants WHERE event_id = :e),
                  (SELECT count(*) FROM ratings r JOIN participants p ON p.id = r.participant_id WHERE p.event_id = :e),
                  (SELECT count(*) FROM event_wines WHERE event_id = :e AND revealed_at IS NOT NULL)""",
        {"e": event["id"]},
    ).fetchone()
    return ":".join(str(x) for x in row)


@router.get("/events/{event_id}/live.json")
def live_json(event=Depends(owned_event), db=Depends(get_db)):
    return JSONResponse({"v": live_version(db, event)})


@router.get("/events/{event_id}/screen")
def screen(request: Request, event=Depends(owned_event), db=Depends(get_db)):
    join_url = f"{public_url(request)}/j/{event['join_code']}"
    data = scoring.tally(db, event["id"])
    progress = {}
    for b in data["ballots"]:
        progress[b["event_wine_id"]] = progress.get(b["event_wine_id"], 0) + 1
    rows = scoring.summarize(data)
    latest = max((r for r in rows if r["wine"]["revealed_at"]), key=lambda r: r["wine"]["revealed_at"], default=None)
    return render(request, "org/screen.html", event=event, join_url=join_url, qr=qr_svg(join_url, 10),
                  wines=data["wines"], progress=progress, people=len(data["participants"]), rows=rows,
                  latest=latest, hidden=sum(r["wine"]["revealed_at"] is None for r in rows),
                  version=live_version(db, event))
