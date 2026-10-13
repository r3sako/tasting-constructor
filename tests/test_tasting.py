"""Сквозные сценарии: то, что прямо требует условие задачи."""

import json

from app import export, scoring
from app.security import sign

from conftest import Organizer, criteria_of, join, rate, wines_of


def results(db, event):
    db.commit()
    data = scoring.tally(db, event["id"])
    return data, scoring.summarize(data)


# ---------- основной путь ----------

def test_open_tasting_from_creation_to_published_results(organizer, new_client, db, slugs):
    event = organizer.create_event(db, slugs[:3])
    assert event["status"] == "voting"
    wines = wines_of(db, event)

    marks = {"Аня": [5, 3, 1], "Боря": [4, 3, 2], "Вера": [5, 4, 1]}
    for i, (name, row) in enumerate(marks.items()):
        guest = new_client(f"phone-{i}")
        assert join(guest, event, name, fp=f"device-{i}").status_code == 200
        for wine, value in zip(wines, row):
            assert rate(guest, db, event, wine, value).status_code == 200

    _, rows = results(db, event)
    assert [r["wine"]["wine_slug"] for r in rows] == slugs[:3]
    assert [r["score"] for r in rows] == [4.67, 3.33, 1.33]
    assert [r["votes"] for r in rows] == [3, 3, 3]

    page = new_client().get(f"/r/{event['public_code']}")
    assert "ещё не опубликованы" in page.text  # до публикации итоги закрыты

    organizer.status(event["id"], "close")
    organizer.status(event["id"], "publish")
    page = new_client().get(f"/r/{event['public_code']}")
    assert page.status_code == 200 and "4,67" in page.text


def test_only_catalog_wines_can_be_added(organizer, db, slugs):
    event = organizer.create_event(db, [slugs[0], "vino-kotorogo-net-v-kataloge"], start=False)
    assert [w["wine_slug"] for w in wines_of(db, event)] == [slugs[0]]


def test_wines_and_scales_are_frozen_after_start(organizer, db, slugs):
    event = organizer.create_event(db, slugs[:2])
    r = organizer.c.post(f"/org/events/{event['id']}/wines/add", data={"slug": slugs[2]})
    assert r.status_code == 409
    r = organizer.c.post(f"/org/events/{event['id']}/scales/preset", data={"key": "pro"})
    assert r.status_code == 409
    assert len(wines_of(db, event)) == 2 and len(criteria_of(db, event)) == 1


def test_foreign_organizer_cannot_open_event(organizer, new_client, db, slugs):
    event = organizer.create_event(db, slugs[:2])
    stranger = Organizer(new_client(), email="other@example.com")
    assert stranger.c.get(f"/org/events/{event['id']}").status_code == 404
    assert stranger.status(event["id"], "close").status_code == 404
    assert new_client().get(f"/org/events/{event['id']}", follow_redirects=False).status_code == 303


# ---------- шкалы ----------

def test_score_is_normalized_to_five_point_scale():
    criteria = [
        {"id": 1, "min_value": 0, "max_value": 5, "weight": 5},
        {"id": 2, "min_value": 0, "max_value": 15, "weight": 15},
        {"id": 3, "min_value": 0, "max_value": 20, "weight": 20},
        {"id": 4, "min_value": 0, "max_value": 10, "weight": 10},
    ]
    assert scoring.ballot_score({1: 0, 2: 0, 3: 0, 4: 0}, criteria) == 1.0
    assert scoring.ballot_score({1: 5, 2: 15, 3: 20, 4: 10}, criteria) == 5.0
    assert scoring.ballot_score({1: 5, 2: 15, 3: 0, 4: 5}, criteria) == 3.0  # 25 баллов из 50
    assert scoring.ballot_score({1: 5, 2: 15, 3: 20}, criteria) is None      # неполная оценка не считается


def test_custom_scales_with_weights(organizer, new_client, db, slugs):
    event = organizer.create_event(db, slugs[:1], start=False)
    r = organizer.c.post(f"/org/events/{event['id']}/scales", data={
        "name": ["Аромат", "Вкус"], "hint": ["", ""], "min": ["1", "0"], "max": ["10", "100"], "weight": ["1", "3"],
    })
    assert r.status_code == 200
    aroma, taste = criteria_of(db, event)
    organizer.status(event["id"], "start")
    guest = new_client()
    join(guest, event)
    wine = wines_of(db, event)[0]
    guest.post(f"/t/{event['join_code']}/w/{wine['id']}", data={f"c_{aroma['id']}": "10", f"c_{taste['id']}": "50"})
    _, rows = results(db, event)
    assert rows[0]["score"] == 3.5  # (1·1,0 + 3·0,5) / 4 = 0,625 → 1 + 4·0,625


def test_value_outside_scale_is_not_saved(organizer, new_client, db, slugs):
    event = organizer.create_event(db, slugs[:1])
    guest = new_client()
    join(guest, event)
    rate(guest, db, event, wines_of(db, event)[0], 9)  # шкала 1–5
    assert results(db, event)[0]["ballots"] == []


# ---------- вход по QR и защита от повторных голосов ----------

def test_adult_confirmation_is_required(organizer, new_client, db, slugs):
    event = organizer.create_event(db, slugs[:1])
    r = new_client().post(f"/j/{event['join_code']}", data={"name": "Юный гость"})
    assert r.status_code == 422
    assert results(db, event)[0]["participants"] == []


def test_repeated_vote_updates_instead_of_adding(organizer, new_client, db, slugs):
    event = organizer.create_event(db, slugs[:2])
    guest = new_client()
    join(guest, event)
    wine = wines_of(db, event)[0]
    for value in (2, 5, 4):
        rate(guest, db, event, wine, value)
    data, rows = results(db, event)
    assert len(data["ballots"]) == 1
    assert rows[0]["score"] == 4.0 and rows[0]["votes"] == 1


def test_returning_guest_keeps_single_participant(organizer, new_client, db, slugs):
    event = organizer.create_event(db, slugs[:1])
    guest = new_client()
    join(guest, event, "Аня")
    join(guest, event, "Аня ещё раз")
    guest.get(f"/j/{event['join_code']}")
    people = results(db, event)[0]["participants"]
    assert [p["name"] for p in people] == ["Аня"]


def test_second_join_from_same_device_is_held_until_organizer_confirms(organizer, new_client, db, slugs):
    event = organizer.create_event(db, slugs[:1])
    wine = wines_of(db, event)[0]
    first, second = new_client("same-phone"), new_client("same-phone")  # режим инкогнито: cookie новые, устройство то же
    join(first, event, "Честный", fp="same-device")
    join(second, event, "Повтор", fp="same-device")
    rate(first, db, event, wine, 3)
    rate(second, db, event, wine, 5)

    data, rows = results(db, event)
    repeat = next(p for p in data["participants"] if p["name"] == "Повтор")
    assert repeat["reason"] == "duplicate_device"
    assert rows[0]["votes"] == 1 and rows[0]["score"] == 3.0

    organizer.c.post(f"/org/events/{event['id']}/participants/{repeat['id']}", data={"action": "confirm"})
    _, rows = results(db, event)
    assert rows[0]["votes"] == 2 and rows[0]["score"] == 4.0


def test_different_devices_are_not_flagged(organizer, new_client, db, slugs):
    event = organizer.create_event(db, slugs[:1])
    join(new_client("phone-a"), event, "А", fp="device-a")
    join(new_client("phone-b"), event, "Б", fp="device-b")
    assert all(p["reason"] is None for p in results(db, event)[0]["participants"])


def test_personal_invite_works_once(organizer, new_client, db, slugs):
    event = organizer.create_event(db, slugs[:1], access="personal")
    organizer.c.post(f"/org/events/{event['id']}/invites", data={"count": "2"})
    db.commit()
    codes = [r["code"] for r in db.execute("SELECT code FROM invites WHERE event_id = ?", (event["id"],))]

    assert join(new_client("a"), event, "Без кода").status_code == 422
    assert join(new_client("b"), event, "С чужим кодом", invite="zzzzzzzzzz").status_code == 422
    assert join(new_client("c"), event, "Первый", invite=codes[0]).status_code == 200
    assert join(new_client("d"), event, "Второй по тому же коду", invite=codes[0]).status_code == 422
    assert [p["name"] for p in results(db, event)[0]["participants"]] == ["Первый"]


def test_seat_limit(organizer, new_client, db, slugs):
    event = organizer.create_event(db, slugs[:1], seats="2")
    assert join(new_client("a"), event, "1", fp="a").status_code == 200
    assert join(new_client("b"), event, "2", fp="b").status_code == 200
    assert join(new_client("c"), event, "3", fp="c").status_code == 422


def test_votes_accepted_only_while_voting_is_open(organizer, new_client, db, slugs):
    event = organizer.create_event(db, slugs[:1], start=False)
    wine = wines_of(db, event)[0]
    guest = new_client()
    join(guest, event)

    rate(guest, db, event, wine, 5)  # черновик: голосование ещё не открыто
    assert results(db, event)[0]["ballots"] == []

    organizer.status(event["id"], "start")
    rate(guest, db, event, wine, 5)
    organizer.status(event["id"], "close")
    rate(guest, db, event, wine, 1)  # после закрытия оценка не меняется
    assert results(db, event)[1][0]["score"] == 5.0
    assert join(new_client("late"), event, "Опоздавший", fp="late").status_code == 422


def test_uniform_extreme_ballots_are_excluded(organizer, new_client, db, slugs):
    event = organizer.create_event(db, slugs[:4])
    wines = wines_of(db, event)
    fan, honest = new_client("fan"), new_client("honest")
    join(fan, event, "Накрутчик", fp="fan")
    join(honest, event, "Честный", fp="honest")
    for i, wine in enumerate(wines):
        rate(fan, db, event, wine, 5)
        rate(honest, db, event, wine, 2 + i % 3)
    data, rows = results(db, event)
    assert {p["name"]: p["reason"] for p in data["participants"]} == {"Накрутчик": "uniform_extreme", "Честный": None}
    assert all(r["votes"] == 1 for r in rows)


def test_organizer_can_exclude_and_restore_guest(organizer, new_client, db, slugs):
    event = organizer.create_event(db, slugs[:1])
    guest = new_client()
    join(guest, event)
    rate(guest, db, event, wines_of(db, event)[0], 4)
    pid = results(db, event)[0]["participants"][0]["id"]
    organizer.c.post(f"/org/events/{event['id']}/participants/{pid}", data={"action": "exclude"})
    assert results(db, event)[1][0]["votes"] == 0
    organizer.c.post(f"/org/events/{event['id']}/participants/{pid}", data={"action": "restore"})
    assert results(db, event)[1][0]["votes"] == 1


# ---------- слепая дегустация ----------

def test_blind_tasting_hides_wines_until_reveal(organizer, new_client, db, slugs):
    event = organizer.create_event(db, slugs[:3], mode="blind")
    wines = wines_of(db, event)
    titles = [db.execute("SELECT title FROM cat.wines WHERE slug = ?", (s,)).fetchone()["title"] for s in slugs[:3]]
    guest = new_client()
    join(guest, event)
    for wine, value in zip(wines, (5, 3, 1)):
        rate(guest, db, event, wine, value)

    def seen():
        html = guest.get(f"/t/{event['join_code']}").text
        html += "".join(guest.get(f"/t/{event['join_code']}/w/{w['id']}").text for w in wines)
        return [t for t in titles if t in html]

    assert seen() == []
    organizer.status(event["id"], "close")
    assert seen() == []  # закрытие само по себе ничего не раскрывает

    # «раскрыть следующий» идёт от последнего места к первому
    organizer.c.post(f"/org/events/{event['id']}/reveal", data={"target": "next"})
    assert seen() == [titles[2]]
    organizer.c.post(f"/org/events/{event['id']}/reveal", data={"target": "next"})
    assert seen() == [titles[1], titles[2]]
    organizer.status(event["id"], "publish")  # публикация раскрывает остальное
    assert seen() == titles


def test_reveal_is_refused_while_voting(organizer, db, slugs):
    event = organizer.create_event(db, slugs[:2], mode="blind")
    organizer.c.post(f"/org/events/{event['id']}/reveal", data={"target": "all"})
    assert all(w["revealed_at"] is None for w in wines_of(db, event))


# ---------- передача в Народный рейтинг ----------

def prepared_event(organizer, new_client, db, slugs):
    event = organizer.create_event(db, slugs[:2])
    wines = wines_of(db, event)
    for i, values in enumerate([(5, 4), (3, 4)]):
        guest = new_client(f"g{i}")
        join(guest, event, f"Гость {i}", fp=f"g{i}")
        for wine, value in zip(wines, values):
            rate(guest, db, event, wine, value)
    cheat = new_client("g0")  # повторный вход с устройства первого гостя
    join(cheat, event, "Повтор", fp="g0")
    rate(cheat, db, event, wines[0], 5)
    return event


def test_export_contains_only_valid_votes(organizer, new_client, db, slugs):
    event = prepared_event(organizer, new_client, db, slugs)
    organizer.status(event["id"], "close")
    payload = organizer.c.get(f"/org/events/{event['id']}/export.json").json()

    assert payload["schema"] == export.SCHEMA_NAME
    assert len(payload["votes"]) == 4
    assert payload["summary"]["valid_votes"] == 4
    assert payload["summary"]["excluded_votes"]["duplicate_device"] == 1
    assert {v["wine_slug"] for v in payload["votes"]} == set(slugs[:2])
    assert all(1 <= v["score"] <= 5 for v in payload["votes"])
    assert "Гость" not in json.dumps(payload, ensure_ascii=False)  # имён в выгрузке нет


def test_transfer_is_refused_before_voting_is_closed(organizer, new_client, db, slugs):
    event = prepared_event(organizer, new_client, db, slugs)
    organizer.c.post(f"/org/events/{event['id']}/transfer")
    db.commit()
    assert db.execute("SELECT count(*) FROM received_votes").fetchone()[0] == 0


def test_repeated_transfer_does_not_duplicate_votes(organizer, new_client, db, slugs):
    event = prepared_event(organizer, new_client, db, slugs)
    organizer.status(event["id"], "close")
    organizer.c.post(f"/org/events/{event['id']}/transfer")
    organizer.c.post(f"/org/events/{event['id']}/transfer")
    db.commit()
    log = db.execute("SELECT accepted, duplicates FROM transfers ORDER BY id").fetchall()
    assert [tuple(r) for r in log] == [(4, 0), (0, 4)]
    assert db.execute("SELECT count(*) FROM received_votes").fetchone()[0] == 4


def test_receiver_checks_signature_and_content(new_client, db, slugs):
    client = new_client()
    url = "/mock/narodny-rating/v1/votes"

    def send(payload, signature=None):
        body = export.encode(payload)
        return client.post(url, content=body, headers={"X-Signature": signature or "sha256=" + sign(body)})

    good = {"vote_id": "a" * 32, "wine_slug": slugs[0], "score": 4.5}
    payload = {"schema": export.SCHEMA_NAME, "source": {"event_id": "x"}, "votes": [
        good,
        {"vote_id": "b" * 32, "wine_slug": "net-takogo-vina", "score": 4},
        {"vote_id": "c" * 32, "wine_slug": slugs[0], "score": 7},
        {"vote_id": "короткий", "wine_slug": slugs[0], "score": 3},
    ]}
    assert send(payload, "sha256=" + "0" * 64).status_code == 401
    answer = send(payload).json()
    assert answer["accepted"] == 1
    assert [r["reason"] for r in answer["rejected"]] == ["unknown wine", "score out of range", "bad vote_id"]
    assert send(payload).json()["duplicates"] == 1
    assert client.get(f"/mock/narodny-rating/v1/wines/{slugs[0]}").json()["votes_from_tastings"] == 1


# ---------- прочее ----------

def test_cross_site_form_post_is_rejected(organizer, db, slugs):
    event = organizer.create_event(db, slugs[:1])
    r = organizer.c.post(f"/org/events/{event['id']}/status", data={"action": "close"},
                         headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert Organizer.event(db, event["id"])["status"] == "voting"


def test_catalog_search_ignores_case_of_cyrillic(organizer, db, slugs):
    event = organizer.create_event(db, [], start=False)
    page = organizer.c.get(f"/org/events/{event['id']}/wines", params={"q": "каберне"})
    assert page.status_code == 200 and "Ничего не нашлось" not in page.text
