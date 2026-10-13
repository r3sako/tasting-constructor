"""Собирает из data/raw/ нормализованный каталог: data/catalog.sqlite и data/catalog.jsonl.

Ничего не додумывает: поле, которого нет в карточке, остаётся NULL.

    python3 scripts/build_catalog.py
"""

import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
RAW = DATA / "raw"

SCHEMA = """
CREATE TABLE wines (
    slug              TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    manufacturer      TEXT,
    manufacturer_slug TEXT,
    region            TEXT,
    category          TEXT,   -- как в каталоге: «Красное сухое»
    color             TEXT,   -- Красное / Белое / Розовое / Оранжевое
    sweetness         TEXT,   -- сухое / полусухое / полусладкое / сладкое / брют / экстра брют
    shade             TEXT,   -- оттенок из карточки: «Рубиновый»
    alcohol           REAL,
    alcohol_max       REAL,
    temp_min          INTEGER,
    temp_max          INTEGER,
    description       TEXT,
    public_rating     REAL,   -- народный рейтинг платформы
    image             TEXT,
    url               TEXT NOT NULL,
    fetched_at        TEXT NOT NULL
);
CREATE TABLE wine_grapes (slug TEXT REFERENCES wines, grape TEXT, PRIMARY KEY (slug, grape));
CREATE TABLE wine_dishes (slug TEXT REFERENCES wines, dish TEXT, PRIMARY KEY (slug, dish));
CREATE INDEX wines_filter ON wines (color, sweetness, region);
CREATE INDEX grapes_grape ON wine_grapes (grape);
CREATE INDEX dishes_dish ON wine_dishes (dish);
"""

COLORS = ("Красное", "Белое", "Розовое", "Оранжевое")


def name(obj):
    return (obj or {}).get("name") or None


def split_category(category):
    if not category:
        return None, None
    for color in COLORS:
        if category.startswith(color + " "):
            return color, category[len(color) + 1 :].strip().lower()
    return None, None


def parse_temperature(value):
    nums = [int(n) for n in re.findall(r"\d+", value or "")]
    if len(nums) == 1:
        return nums[0], nums[0]
    if len(nums) == 2 and nums[0] <= nums[1]:
        return nums[0], nums[1]
    return None, None


def normalize(record, list_ratings):
    w = record["wine"]
    category = name(w.get("category"))
    color, sweetness = split_category(category)
    temp_min, temp_max = parse_temperature(w.get("temperature"))
    rating = w.get("publicRating")
    if rating is None:
        rating = list_ratings.get(w["slug"])
    manufacturer = w.get("manufacturer") or {}
    return {
        "slug": w["slug"],
        "title": w["title"].strip(),
        "manufacturer": manufacturer.get("name"),
        "manufacturer_slug": manufacturer.get("slug"),
        "region": name(w.get("region")),
        "category": category,
        "color": color,
        "sweetness": sweetness,
        "shade": (w.get("color") or "").strip() or None,
        "alcohol": w.get("alcohol"),
        "alcohol_max": w.get("alcoholMax"),
        "temp_min": temp_min,
        "temp_max": temp_max,
        "description": (w.get("description") or "").strip() or None,
        "public_rating": rating,
        "image": (w.get("image") or {}).get("url"),
        "url": record["url"],
        "fetched_at": record["fetched_at"],
        # dict.fromkeys убирает повторы, сохраняя порядок из карточки
        "grapes": list(dict.fromkeys(n for g in w.get("grapes") or [] if (n := name(g)))),
        "dishes": list(dict.fromkeys(n for d in w.get("dishes") or [] if (n := name(d)))),
    }


def main() -> int:
    ratings_file = RAW / "public_ratings.json"
    list_ratings = json.loads(ratings_file.read_text(encoding="utf-8")) if ratings_file.exists() else {}
    wines = [
        normalize(json.loads(p.read_text(encoding="utf-8")), list_ratings)
        for p in sorted((RAW / "wines").glob("*.json"))
    ]
    wines.sort(key=lambda w: w["slug"])

    with (DATA / "catalog.jsonl").open("w", encoding="utf-8") as f:
        for w in wines:
            f.write(json.dumps(w, ensure_ascii=False) + "\n")

    db_path = DATA / "catalog.sqlite"
    db_path.unlink(missing_ok=True)
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    columns = [c for c in wines[0] if c not in ("grapes", "dishes")]
    db.executemany(
        f"INSERT INTO wines ({', '.join(columns)}) VALUES ({', '.join(':' + c for c in columns)})", wines
    )
    db.executemany("INSERT INTO wine_grapes VALUES (?, ?)", [(w["slug"], g) for w in wines for g in w["grapes"]])
    db.executemany("INSERT INTO wine_dishes VALUES (?, ?)", [(w["slug"], d) for w in wines for d in w["dishes"]])
    db.commit()
    db.close()

    print(f"вин: {len(wines)}")
    for field in ("color", "sweetness", "region"):
        print(f"{field}: {Counter(w[field] for w in wines).most_common(12)}")
    print("пустые поля:", {c: n for c in columns if (n := sum(w[c] is None for w in wines))})
    print("без сортов:", sum(not w["grapes"] for w in wines), "| без гастросочетаний:", sum(not w["dishes"] for w in wines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
