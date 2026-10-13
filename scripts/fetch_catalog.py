"""Скачивает каталог «Своё Вино» (vino-svoe.ru) в data/raw/.

Источник правды — публичные страницы /wines/<slug> из wines-sitemap.xml.
Данные карточки берутся из встроенного в страницу __NUXT_DATA__, а не из вёрстки.
Уже скачанные карточки пропускаются, так что скрипт можно перезапускать.

    python3 scripts/fetch_catalog.py            # докачать недостающее
    python3 scripts/fetch_catalog.py --limit 20 # проба на 20 карточках
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://vino-svoe.ru"
RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
WINES_DIR = RAW / "wines"
USER_AGENT = "Mozilla/5.0 (compatible; svoe-vino-sommelier-prototype; student case)"
WORKERS = 4
DELAY = 0.2  # пауза после каждого запроса в каждом потоке

# Обёртки devalue/Nuxt: ["Reactive", <index>] и т.п.
WRAPPERS = {"Reactive", "ShallowReactive", "Ref", "ShallowRef", "EmptyRef", "EmptyShallowRef"}
NUXT_DATA_RE = re.compile(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S)


def get(url: str, retries: int = 4) -> str:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        time.sleep(2 ** attempt)
    raise last


def nuxt_data(html: str) -> dict:
    """Разворачивает плоский payload Nuxt в обычный dict и возвращает его раздел data."""
    flat = json.loads(NUXT_DATA_RE.search(html).group(1))

    def resolve(i, depth=0):
        if not isinstance(i, int) or isinstance(i, bool):  # литерал внутри служебной обёртки (Date, Set…)
            return i
        if i < 0 or depth > 20:  # отрицательные индексы — undefined/null
            return None
        v = flat[i]
        if isinstance(v, dict):
            return {k: resolve(x, depth + 1) for k, x in v.items()}
        if isinstance(v, list):
            if v and isinstance(v[0], str) and v[0] in WRAPPERS:
                return resolve(v[1], depth + 1) if len(v) > 1 else None
            return [resolve(x, depth + 1) for x in v]
        return v

    return resolve(0)["data"]


def sitemap_slugs() -> list[str]:
    xml = get(f"{BASE}/wines-sitemap.xml")
    return [urllib.parse.unquote(s) for s in re.findall(r"<loc>https://vino-svoe\.ru/wines/([^<]+)</loc>", xml)]


def wine_path(slug: str) -> Path:
    return WINES_DIR / (urllib.parse.quote(slug, safe="") + ".json")


def fetch_wine(slug: str) -> str:
    url = f"{BASE}/wines/{urllib.parse.quote(slug)}"
    try:
        data = nuxt_data(get(url))
    except urllib.error.HTTPError as e:
        return f"http {e.code}"
    finally:
        time.sleep(DELAY)
    wine = next((v.get("wine") for v in data.values() if isinstance(v, dict) and v.get("wine")), None)
    if not wine:
        return "no wine in payload"
    record = {"url": url, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "wine": wine}
    wine_path(slug).write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return "ok"


def fetch_ratings() -> dict[str, float]:
    """Народный рейтинг есть только в списке каталога, поэтому обходим его страницы."""

    def page(n: int) -> dict:
        try:
            data = nuxt_data(get(f"{BASE}/wines?page={n}"))
        finally:
            time.sleep(DELAY)
        return next(v for v in data.values() if isinstance(v, dict) and "items" in v)

    first = page(1)
    pages = [first]
    with ThreadPoolExecutor(WORKERS) as pool:
        pages += list(pool.map(page, range(2, first["totalPages"] + 1)))
    ratings = {}
    seen = set()
    for p in pages:
        for item in p["items"]:
            seen.add(item["slug"])
            if item.get("publicRating") is not None:
                ratings[item["slug"]] = item["publicRating"]
    print(f"список каталога: {len(seen)} вин из {first['totalItems']}, с народным рейтингом: {len(ratings)}")
    return ratings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="скачать не больше N карточек (для пробы)")
    ap.add_argument("--skip-ratings", action="store_true")
    args = ap.parse_args()

    WINES_DIR.mkdir(parents=True, exist_ok=True)
    slugs = sitemap_slugs()
    todo = [s for s in slugs if not wine_path(s).exists()][: args.limit]
    print(f"в sitemap: {len(slugs)}, уже скачано: {len(slugs) - len([s for s in slugs if not wine_path(s).exists()])}, качаем: {len(todo)}")

    failed = {}
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = {pool.submit(fetch_wine, s): s for s in todo}
        for n, fut in enumerate(as_completed(futures), 1):
            slug = futures[fut]
            try:
                status = fut.result()
            except Exception as e:  # одна битая карточка не должна ронять весь прогон
                status = f"{type(e).__name__}: {e}"
            if status != "ok":
                failed[slug] = status
            if n % 200 == 0:
                print(f"  {n}/{len(todo)}", flush=True)

    (RAW / "failed.json").write_text(json.dumps(failed, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"готово: ok {len(todo) - len(failed)}, ошибок {len(failed)} (см. data/raw/failed.json)")

    if not args.skip_ratings:
        ratings = fetch_ratings()
        (RAW / "public_ratings.json").write_text(json.dumps(ratings, ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
