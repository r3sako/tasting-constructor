"""Общее для всех страниц: шаблоны, сообщения, вход организатора."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .db import get_db

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

IMAGE_BASE = "https://api.vino-svoe.ru/v1/img/str-api/400/400/resize"
MSK = timezone(timedelta(hours=3))

STATUS = {
    "draft": "Черновик",
    "voting": "Идёт голосование",
    "closed": "Голосование закрыто",
    "published": "Итоги опубликованы",
}
MODE = {"open": "Открытая", "blind": "Слепая"}
ACCESS = {"link": "Общий QR-код", "personal": "Одноразовые приглашения"}


def _dt(value, fmt="%d.%m.%Y %H:%M"):
    if not value:
        return ""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo:  # служебные отметки хранятся в UTC, показываем по Москве
        parsed = parsed.astimezone(MSK)
    return parsed.strftime(fmt)


def _score(value, digits=2):
    return "—" if value is None else f"{value:.{digits}f}".replace(".", ",")


def _num(value):
    """Число без хвоста «.0»: вес 1.0 показываем как 1."""
    return f"{value:g}".replace(".", ",") if value is not None else ""


def _plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


templates.env.filters.update(dt=_dt, score=_score, num=_num)
templates.env.globals.update(
    STATUS=STATUS, MODE=MODE, ACCESS=ACCESS, plural=_plural,
    image=lambda path: IMAGE_BASE + path if path else None,
)


def flash(request: Request, text: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, text])


def render(request: Request, template: str, /, status_code: int = 200, **context):
    context["flashes"] = request.session.pop("flash", [])
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def public_url(request: Request) -> str:
    """Адрес, который попадёт в QR-код. За прокси или при демо в локальной сети задаётся PUBLIC_URL."""
    return (os.environ.get("PUBLIC_URL") or str(request.base_url)).rstrip("/")


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def organizer(request: Request, db=Depends(get_db)):
    org_id = request.session.get("org_id")
    row = db.execute("SELECT * FROM organizers WHERE id = ?", (org_id,)).fetchone() if org_id else None
    if not row:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return row


def owned_event(event_id: int, org=Depends(organizer), db=Depends(get_db)):
    event = db.execute(
        "SELECT * FROM events WHERE id = ? AND organizer_id = ?", (event_id, org["id"])
    ).fetchone()
    if not event:
        raise HTTPException(status_code=404, detail="Дегустация не найдена")
    return event
