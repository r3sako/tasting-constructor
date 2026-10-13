"""Наполняет демо-кабинет четырьмя дегустациями на разных этапах.

Вход в демо-кабинет — кнопкой на главной при запуске с DEMO=1, пароль не нужен.
Вина берутся из каталога; оценки гостей сгенерированы (зерно фиксировано) и к реальным людям не относятся.

    python3 scripts/seed_demo.py          # создать, если демо ещё нет
    python3 scripts/seed_demo.py --reset  # пересоздать демо-данные
"""

import argparse
import random
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import export  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.org import set_preset  # noqa: E402
from app.public import DEMO_EMAIL  # noqa: E402
from app.security import hash_password, new_code, new_token, sha256  # noqa: E402

rng = random.Random(2026)
NAMES = ["Анна", "Борис", "Вера", "Глеб", "Дарья", "Егор", "Жанна", "Захар", "Ирина", "Кирилл",
         "Лидия", "Марк", "Нина", "Олег", "Полина", "Роман", "Софья", "Тимур"]
NOTES = ["Яркая вишня и немного специй", "Свежее, хочется второй бокал", "Танины жестковаты", "К мясу на гриле — отлично",
         "Аромат раскрылся минут через десять", "Простовато, но чисто", "Долгое послевкусие", "Мой фаворит вечера",
         "Кислотность высокая, к рыбе", "Чувствуется дуб", "Лёгкое и питкое", "Немного не хватает тела"]


MSK = timezone(timedelta(hours=3))
NOW = datetime.now(MSK).replace(second=0, microsecond=0)


def at(start: datetime, minutes: int = 0) -> str:
    """Служебная отметка времени (UTC) через столько-то минут после начала дегустации."""
    return (start + timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat(timespec="seconds")


def pick_wines(db, where: str, args: tuple, count: int) -> list[str]:
    """Вина с народным рейтингом ниже 5,00 (у «пятёрок» обычно один голос) и не более двух от одного производителя, чтобы программа была разнообразной."""
    rows = db.execute(
        f"SELECT slug, manufacturer FROM cat.wines w WHERE public_rating < 5 AND {where}"
        " ORDER BY public_rating DESC, slug", args,
    ).fetchall()
    picked, per_maker = [], {}
    for row in rows:
        if per_maker.get(row["manufacturer"], 0) < 2:
            picked.append(row["slug"])
            per_maker[row["manufacturer"]] = per_maker.get(row["manufacturer"], 0) + 1
        if len(picked) == count:
            break
    return picked


def create_event(db, org_id, *, title, description, place, start, mode, access, seats, preset, slugs, flights=None):
    cursor = db.execute(
        """INSERT INTO events (organizer_id, title, description, place, starts_at, mode, access, seats,
                               join_code, public_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (org_id, title, description, place, start.replace(tzinfo=None).isoformat(timespec="minutes"), mode, access,
         seats, new_code(8), new_code(10), at(min(start, NOW), -3 * 24 * 60)),
    )
    event_id = cursor.lastrowid
    set_preset(db, event_id, preset)
    for position, slug in enumerate(slugs, 1):
        flight = flights[(position - 1) * len(flights) // len(slugs)] if flights else None
        db.execute("INSERT INTO event_wines (event_id, wine_slug, position, flight) VALUES (?, ?, ?, ?)",
                   (event_id, slug, position, flight))
    return event_id


def add_guests(db, event_id, count, *, start, share=1.0):
    """Гости с правдоподобными оценками: у каждого вина своё «качество», у каждого гостя — своя строгость."""
    wines = db.execute("SELECT id FROM event_wines WHERE event_id = ? ORDER BY position", (event_id,)).fetchall()
    criteria = db.execute("SELECT * FROM criteria WHERE event_id = ? ORDER BY position", (event_id,)).fetchall()
    quality = {w["id"]: rng.uniform(0.45, 0.9) for w in wines}
    ids = []
    for n, name in enumerate(rng.sample(NAMES, count)):
        cursor = db.execute(
            """INSERT INTO participants (event_id, token_hash, name, adult_confirmed, device_hash, ip_hash, joined_at)
               VALUES (?, ?, ?, 1, ?, ?, ?)""",
            (event_id, sha256(new_token()), name, sha256(f"demo-device-{event_id}-{n}")[:32],
             sha256(f"demo-ip-{event_id}")[:32], at(start, rng.randint(1, 12))),
        )
        ids.append(cursor.lastrowid)
        strictness = rng.uniform(-0.12, 0.12)
        for wine in wines:
            if rng.random() > share:
                continue
            rated = at(start, rng.randint(15, 55))
            rating = db.execute(
                "INSERT INTO ratings (participant_id, event_wine_id, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (ids[-1], wine["id"], rng.choice(NOTES) if rng.random() < 0.3 else None, rated, rated),
            ).lastrowid
            for c in criteria:
                level = min(1, max(0, quality[wine["id"]] + strictness + rng.gauss(0, 0.13)))
                value = round(c["min_value"] + level * (c["max_value"] - c["min_value"]))
                db.execute("INSERT INTO rating_values VALUES (?, ?, ?)", (rating, c["id"], value))
    return ids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="удалить прежний демо-кабинет и создать заново")
    args = ap.parse_args()

    init_db()
    db = connect()
    existing = db.execute("SELECT id FROM organizers WHERE email = ?", (DEMO_EMAIL,)).fetchone()
    if existing and not args.reset:
        print("демо-кабинет уже есть; для пересоздания запустите с --reset")
        return 0
    if existing:
        # вместе с демо-событиями убираем и то, что заглушка рейтинга приняла от них
        db.execute("DELETE FROM received_votes WHERE source_event IN"
                   " (SELECT public_code FROM events WHERE organizer_id = ?)", (existing["id"],))
        db.execute("DELETE FROM events WHERE organizer_id = ?", (existing["id"],))
        db.execute("DELETE FROM organizers WHERE id = ?", (existing["id"],))

    # пароль случайный и нигде не сохраняется: в демо-кабинет входят кнопкой, а не по паролю
    org_id = db.execute(
        "INSERT INTO organizers (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (DEMO_EMAIL, "Винный клуб «Терруар»", hash_password(secrets.token_urlsafe(24)), at(NOW, -30 * 24 * 60)),
    ).lastrowid

    # 1. Слепая дегустация: завершена, раскрыта, опубликована, оценки переданы в рейтинг
    past = (NOW - timedelta(days=6)).replace(hour=19, minute=0)
    crimea = create_event(
        db, org_id, title="Красные вина Крыма вслепую", place="Винотека «Терруар», Москва", start=past,
        description="Шесть сухих красных из Крыма под номерами. Угадаем, где автохтон, а где классика?",
        mode="blind", access="link", seats=20, preset="senses",
        slugs=pick_wines(db, "region = ? AND color = ? AND sweetness = ?", ("Крым", "Красное", "сухое"), 6),
        flights=["Первый сет", "Второй сет"],
    )
    guests = add_guests(db, crimea, 12, start=past)
    db.execute("UPDATE participants SET duplicate_of = ? WHERE id = ?", (guests[0], guests[-1]))
    db.execute("UPDATE participants SET excluded = 1 WHERE id = ?", (guests[-2],))
    db.execute(
        "UPDATE events SET status = 'published', opened_at = ?, closed_at = ?, published_at = ? WHERE id = ?",
        (at(past), at(past, 60), at(past, 80), crimea),
    )
    db.execute("UPDATE event_wines SET revealed_at = ? WHERE event_id = ?", (at(past, 75), crimea))
    export.transfer(db, db.execute("SELECT * FROM events WHERE id = ?", (crimea,)).fetchone())

    # 2. Слепая дегустация: голосование закрыто, бутылки ещё не раскрыты — можно показать раскрытие
    earlier = NOW - timedelta(minutes=90)
    whites = create_event(
        db, org_id, title="Белые сухие: слепой сет", place="Винотека «Терруар», Москва", start=earlier,
        description="Пять белых сухих из разных регионов. Названия откроем в конце вечера.",
        mode="blind", access="link", seats=None, preset="stars",
        slugs=pick_wines(db, "color = ? AND sweetness = ?", ("Белое", "сухое"), 5),
    )
    add_guests(db, whites, 9, start=earlier)
    db.execute("UPDATE events SET status = 'closed', opened_at = ?, closed_at = ? WHERE id = ?",
               (at(earlier), at(earlier, 60), whites))

    # 3. Открытая дегустация: идёт голосование, к ней можно присоединиться по QR
    recent = NOW - timedelta(minutes=60)
    sparkling = create_event(
        db, org_id, title="Игристые Кубани: брют", place="Краснодар, гастробар «Лоза»", start=recent,
        description="Сравниваем брюты кубанских хозяйств. Оценивайте по ходу подачи.",
        mode="open", access="link", seats=30, preset="pro",
        slugs=pick_wines(db, "region = ? AND sweetness IN ('брют', 'экстра брют')", ("Кубань",), 5),
    )
    add_guests(db, sparkling, 6, start=recent, share=0.6)
    db.execute("UPDATE events SET status = 'voting', opened_at = ? WHERE id = ?", (at(recent), sparkling))

    # 4. Черновик с одноразовыми приглашениями
    don = create_event(
        db, org_id, title="Автохтоны Дона", place="Ростов-на-Дону",
        start=(NOW + timedelta(days=9)).replace(hour=19, minute=0),
        description="Красностоп, Цимлянский чёрный, Сибирьковый — знакомство с донскими сортами.",
        mode="open", access="personal", seats=8, preset="senses",
        slugs=pick_wines(db, "region = ?", ("Долина Дона",), 4),
    )
    for n in range(1, 9):
        db.execute("INSERT INTO invites (event_id, code, label) VALUES (?, ?, ?)", (don, new_code(10), f"Гость {n}"))

    db.commit()
    for row in db.execute("SELECT title, status, mode FROM events WHERE organizer_id = ? ORDER BY id", (org_id,)):
        print(f"  {row['status']:9} {row['mode']:5} {row['title']}")
    print("демо-кабинет готов: запустите приложение с DEMO=1 и нажмите «Посмотреть демо-кабинет»")
    return 0


if __name__ == "__main__":
    sys.exit(main())
