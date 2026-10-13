"""Страницы без кабинета: главная, вход, опубликованные итоги и заглушка приёмника рейтинга."""

import os
import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse

from . import export, scoring
from .db import get_db, now
from .security import hash_password, verify_password
from .web import flash, redirect, render

router = APIRouter()

DEMO_EMAIL = "demo@tasting.local"


def demo_enabled() -> bool:
    return os.environ.get("DEMO") == "1"


@router.get("/")
def index(request: Request):
    if request.session.get("org_id"):
        return redirect("/org")
    return render(request, "index.html", demo=demo_enabled())


@router.get("/login")
def login_page(request: Request):
    return render(request, "login.html", email="", demo=demo_enabled())


@router.post("/login")
def login(request: Request, db=Depends(get_db), email: str = Form(""), password: str = Form("")):
    row = db.execute("SELECT * FROM organizers WHERE email = ?", (email.strip().lower(),)).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        flash(request, "Неверная почта или пароль", "error")
        return render(request, "login.html", status_code=401, email=email, demo=demo_enabled())
    request.session["org_id"] = row["id"]
    return redirect("/org")


@router.post("/login/demo")
def login_demo(request: Request, db=Depends(get_db)):
    """Вход в заранее наполненный демо-кабинет без пароля; работает только при DEMO=1."""
    row = db.execute("SELECT id FROM organizers WHERE email = ?", (DEMO_EMAIL,)).fetchone()
    if not demo_enabled() or not row:
        raise HTTPException(status_code=404)
    request.session["org_id"] = row["id"]
    return redirect("/org")


@router.get("/register")
def register_page(request: Request):
    return render(request, "register.html", form={})


@router.post("/register")
def register(request: Request, db=Depends(get_db), name: str = Form(""), email: str = Form(""),
             password: str = Form("")):
    name, email = name.strip()[:80], email.strip().lower()[:120]
    error = None
    if not name:
        error = "Укажите имя или название площадки"
    elif "@" not in email or "." not in email.split("@")[-1]:
        error = "Проверьте адрес почты"
    elif len(password) < 8:
        error = "Пароль — не короче 8 символов"
    if not error:
        try:
            cursor = db.execute(
                "INSERT INTO organizers (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (email, name, hash_password(password), now()),
            )
        except sqlite3.IntegrityError:
            error = "Кабинет с такой почтой уже есть — войдите"
    if error:
        flash(request, error, "error")
        return render(request, "register.html", status_code=422, form={"name": name, "email": email})
    request.session["org_id"] = cursor.lastrowid
    return redirect("/org")


@router.post("/logout")
def logout(request: Request):
    request.session.pop("org_id", None)
    return redirect("/")


@router.get("/r/{public_code}")
def public_results(request: Request, public_code: str, db=Depends(get_db)):
    event = db.execute("SELECT * FROM events WHERE public_code = ?", (public_code,)).fetchone()
    if not event:
        raise HTTPException(status_code=404, detail="Страница итогов не найдена")
    if event["status"] != "published":
        return render(request, "public/pending.html", event=event)
    data = scoring.tally(db, event["id"])
    return render(request, "public/results.html", event=event, rows=scoring.summarize(data),
                  criteria=data["criteria"], counts=scoring.validity_counts(data),
                  voters=sum(p["valid_ballots"] > 0 for p in data["participants"]))


# ---------- заглушка приёмника Народного рейтинга ----------

@router.post("/mock/narodny-rating/v1/votes")
async def mock_receive(request: Request, db=Depends(get_db)):
    status, answer = export.receive(db, await request.body(), request.headers.get("x-signature", ""))
    return JSONResponse(answer, status_code=status)


@router.get("/mock/narodny-rating/v1/wines/{slug}")
def mock_wine(slug: str, db=Depends(get_db)):
    row = db.execute(
        "SELECT count(*) AS votes, round(avg(score), 2) AS score FROM received_votes WHERE wine_slug = ?", (slug,)
    ).fetchone()
    return {"wine_slug": slug, "votes_from_tastings": row["votes"], "average_score": row["score"]}


@router.get("/healthz")
def healthz(db=Depends(get_db)):
    return {"ok": True, "catalog_wines": db.execute("SELECT count(*) FROM cat.wines").fetchone()[0]}
