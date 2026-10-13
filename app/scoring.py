"""Шкалы, приведение оценок к пятибалльной шкале и отбор валидных оценок."""

from statistics import mean, pstdev

# (название, подсказка, минимум, максимум, вес)
PRESETS = {
    "stars": {
        "title": "Народная",
        "about": "Одна оценка от 1 до 5, как в Народном рейтинге платформы",
        "criteria": [("Общая оценка", "Насколько вам понравилось это вино", 1, 5, 1)],
    },
    "senses": {
        "title": "Любительская",
        "about": "Четыре критерия по десятибалльной шкале",
        "criteria": [
            ("Аромат", "Чистота, яркость, сложность", 1, 10, 1),
            ("Вкус", "Баланс кислотности, сладости и танинов", 1, 10, 1),
            ("Послевкусие", "Длительность и приятность", 1, 10, 1),
            ("Общее впечатление", "Хочется ли ещё бокал", 1, 10, 1),
        ],
    },
    "pro": {
        "title": "Профессиональная",
        "about": "По мотивам 100-балльной системы: вес критерия равен его диапазону",
        "criteria": [
            ("Внешний вид", "Прозрачность, цвет, блеск", 0, 5, 5),
            ("Аромат", "Интенсивность, чистота, сложность", 0, 15, 15),
            ("Вкус", "Структура, баланс, послевкусие", 0, 20, 20),
            ("Общее впечатление", "Гармония и потенциал", 0, 10, 10),
        ],
    },
}

REASONS = {
    "excluded_by_organizer": "исключён организатором",
    "duplicate_device": "повторный вход с того же устройства",
    "uniform_extreme": "всем винам поставлены одинаковые крайние оценки",
    "incomplete": "оценка заполнена не полностью",
}

UNIFORM_MIN_BALLOTS = 4  # с какого числа оценок проверяем «всем по максимуму / всем по минимуму»
TARGET_MIN, TARGET_MAX = 1, 5  # шкала Народного рейтинга


def ballot_score(values: dict, criteria) -> float | None:
    """Взвешенное среднее нормированных критериев, приведённое к шкале 1–5.

    None, если заполнены не все критерии: неполная оценка в расчёт не идёт.
    """
    if not criteria or any(c["id"] not in values for c in criteria):
        return None
    total = sum(c["weight"] for c in criteria)
    share = sum(
        c["weight"] * (values[c["id"]] - c["min_value"]) / (c["max_value"] - c["min_value"]) for c in criteria
    ) / total
    return round(TARGET_MIN + (TARGET_MAX - TARGET_MIN) * share, 2)


def _is_uniform_extreme(ballots, criteria) -> bool:
    complete = [b for b in ballots if b["score"] is not None]
    if len(complete) < UNIFORM_MIN_BALLOTS:
        return False
    all_min = all(b["values"][c["id"]] == c["min_value"] for b in complete for c in criteria)
    all_max = all(b["values"][c["id"]] == c["max_value"] for b in complete for c in criteria)
    return all_min or all_max


def tally(db, event_id: int) -> dict:
    """Собирает всё о дегустации: вина, критерии, участников и оценки с признаком валидности."""
    criteria = db.execute("SELECT * FROM criteria WHERE event_id = ? ORDER BY position, id", (event_id,)).fetchall()
    wines = db.execute(
        """SELECT ew.*, w.title, w.manufacturer, w.region, w.category, w.color, w.image, w.url, w.public_rating
           FROM event_wines ew LEFT JOIN cat.wines w ON w.slug = ew.wine_slug
           WHERE ew.event_id = ? ORDER BY ew.position, ew.id""",
        (event_id,),
    ).fetchall()
    people = [dict(p) for p in db.execute(
        "SELECT * FROM participants WHERE event_id = ? ORDER BY joined_at, id", (event_id,)
    )]
    rows = db.execute(
        """SELECT r.id, r.participant_id, r.event_wine_id, r.note, r.updated_at
           FROM ratings r JOIN participants p ON p.id = r.participant_id
           WHERE p.event_id = ?""",
        (event_id,),
    ).fetchall()
    values = {}
    for v in db.execute(
        """SELECT rv.rating_id, rv.criterion_id, rv.value
           FROM rating_values rv JOIN ratings r ON r.id = rv.rating_id
           JOIN participants p ON p.id = r.participant_id WHERE p.event_id = ?""",
        (event_id,),
    ):
        values.setdefault(v["rating_id"], {})[v["criterion_id"]] = v["value"]

    ballots = []
    for r in rows:
        vals = values.get(r["id"], {})
        ballots.append({**dict(r), "values": vals, "score": ballot_score(vals, criteria), "reason": None})

    by_person = {}
    for b in ballots:
        by_person.setdefault(b["participant_id"], []).append(b)

    for p in people:
        mine = by_person.get(p["id"], [])
        if p["excluded"]:
            p["reason"] = "excluded_by_organizer"
        elif p["duplicate_of"]:
            p["reason"] = "duplicate_device"
        elif _is_uniform_extreme(mine, criteria):
            p["reason"] = "uniform_extreme"
        else:
            p["reason"] = None
        p["ballots"] = len(mine)
        for b in mine:
            b["reason"] = p["reason"] or ("incomplete" if b["score"] is None else None)
        p["valid_ballots"] = sum(b["reason"] is None for b in mine)

    return {"criteria": criteria, "wines": wines, "participants": people, "ballots": ballots}


def summarize(data: dict) -> list[dict]:
    """Итог по каждому вину только по валидным оценкам; сортировка по среднему баллу."""
    valid = {}
    for b in data["ballots"]:
        if b["reason"] is None:
            valid.setdefault(b["event_wine_id"], []).append(b)
    result = []
    for w in data["wines"]:
        votes = valid.get(w["id"], [])
        scores = [b["score"] for b in votes]
        result.append({
            "wine": w,
            "votes": len(votes),
            "score": round(mean(scores), 2) if scores else None,
            "spread": round(pstdev(scores), 2) if len(scores) > 1 else None,
            "criteria": {
                c["id"]: round(mean(b["values"][c["id"]] for b in votes), 1) if votes else None
                for c in data["criteria"]
            },
            "notes": [b["note"] for b in votes if b["note"]],
        })
    result.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0), r["wine"]["position"]))
    place = 0
    for r in result:
        if r["score"] is not None:
            place += 1
            r["place"] = place
        else:
            r["place"] = None
    return result


def validity_counts(data: dict) -> dict:
    counts = {"valid": 0, **{reason: 0 for reason in REASONS}}
    for b in data["ballots"]:
        counts[b["reason"] or "valid"] += 1
    return counts
