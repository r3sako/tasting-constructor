"""Конструктор дегустаций — прототип для платформы «Своё Вино».

    uvicorn app.main:app --reload
"""

import os
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import guest, org, public
from .db import init_db
from .security import secret_key
from .web import render

init_db()

app = FastAPI(title="Конструктор дегустаций", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(public.router)
app.include_router(org.router)
app.include_router(guest.router)


@app.middleware("http")
async def same_origin_posts(request: Request, call_next):
    """Защита форм от подделки запроса: POST из браузера принимаем только со своего адреса.

    Приёмник рейтинга исключён — к нему обращаются серверы, и он проверяет подпись.
    """
    origin = request.headers.get("origin")
    if request.method == "POST" and origin and not request.url.path.startswith("/mock/"):
        allowed = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
        public = os.environ.get("PUBLIC_URL")
        if public:
            allowed.add(urlparse(public).netloc)
        if urlparse(origin).netloc not in allowed:
            return PlainTextResponse("Запрос с чужого сайта отклонён", status_code=403)
    return await call_next(request)


# добавляется после same_origin_posts, поэтому оборачивает его: сессия доступна во всех обработчиках
app.add_middleware(SessionMiddleware, secret_key=secret_key(), session_cookie="tc_session",
                   same_site="lax", max_age=14 * 86400)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if exc.status_code in (301, 302, 303, 307):
        return PlainTextResponse("", status_code=exc.status_code, headers=exc.headers)
    titles = {404: "Страница не найдена", 409: "Действие сейчас недоступно", 403: "Нет доступа"}
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail != "Not Found" else None
    return render(request, "error.html", status_code=exc.status_code,
                  title=titles.get(exc.status_code, "Что-то пошло не так"), detail=detail)
