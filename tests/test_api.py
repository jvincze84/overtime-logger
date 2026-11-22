import pytest

from app.main import create_app


@pytest.fixture
def client(tmp_path):
    return create_app(str(tmp_path / "test.db")).test_client()


def add(client, start, end, ticket="T-1", description="munka"):
    return client.post(
        "/api/entries",
        json={"ticket": ticket, "description": description, "start": start, "end": end},
    )


def add_saturdays(client, count, hours=4):
    # Consecutive Saturdays from 2026-11-07, 10:00 onwards, all 200%.
    days = ["2026-11-07", "2026-11-14", "2026-11-21", "2026-11-28"]
    ids = []
    for day in days[:count]:
        response = add(client, f"{day}T10:00", f"{day}T{10 + hours}:00", f"T-{day}")
        assert response.status_code == 201
        ids.append(response.get_json()["id"])
    return ids


def compensate(client, day="2026-12-01", hours=8, note=""):
    return client.post("/api/compensations", json={"day": day, "hours": hours, "note": note})


def summary(client):
    return client.get("/api/summary").get_json()


def test_requires_ticket_and_description(client):
    response = client.post(
        "/api/entries",
        json={"ticket": " ", "description": "x", "start": "2026-11-10T18:00", "end": "2026-11-10T19:00"},
    )
    assert response.status_code == 400
    assert "Hibajegy" in response.get_json()["error"]


def test_end_must_follow_start(client):
    assert add(client, "2026-11-10T19:00", "2026-11-10T18:00").status_code == 400


def test_rejects_absurd_duration(client):
    assert add(client, "2026-11-10T19:00", "2027-11-10T18:00").status_code == 400


def test_entry_is_listed_with_calculation(client):
    add(client, "2026-11-10T21:00", "2026-11-10T23:00")
    entry = client.get("/api/entries?month=2026-11").get_json()[0]
    assert entry["overtime_minutes"] == 120
    assert entry["weighted_minutes"] == 60 * 1.5 + 60 * 1.65
    assert client.get("/api/entries?month=2026-10").get_json() == []


def test_overlap_rejected_but_adjacent_allowed(client):
    add(client, "2026-11-10T18:00", "2026-11-10T20:00")
    assert add(client, "2026-11-10T19:00", "2026-11-10T21:00").status_code == 409
    assert add(client, "2026-11-10T20:00", "2026-11-10T21:00").status_code == 201


def test_entry_can_be_saved_over_itself(client):
    entry_id = add(client, "2026-11-10T18:00", "2026-11-10T20:00").get_json()["id"]
    response = client.put(
        f"/api/entries/{entry_id}",
        json={"ticket": "T-2", "description": "x", "start": "2026-11-10T18:30", "end": "2026-11-10T20:00"},
    )
    assert response.status_code == 200


def test_compensation_cannot_exceed_open_balance(client):
    add_saturdays(client, 1, hours=3)  # 6 weighted hours
    assert compensate(client, hours=6.5).status_code == 409
    assert summary(client)["open_weighted_minutes"] == 360


def test_partial_compensation_in_half_hours(client):
    add_saturdays(client, 1, hours=3)  # 6 weighted hours
    assert compensate(client, hours=1.5).status_code == 201
    result = summary(client)
    assert result["open_weighted_minutes"] == 270
    assert result["compensated_weighted_minutes"] == 90


@pytest.mark.parametrize("hours", [0, 0.25, 1.2, 8.5, -1, "2", True, None])
def test_invalid_compensation_amount(client, hours):
    add_saturdays(client, 4)
    assert compensate(client, hours=hours).status_code == 400


def test_same_day_compensations_add_up(client):
    add_saturdays(client, 2, hours=2)  # 8 weighted hours in total
    first = compensate(client, hours=3, note="reggel").get_json()["id"]
    second = compensate(client, hours=2, note="délután").get_json()["id"]
    assert first == second
    comps = client.get("/api/compensations").get_json()
    assert len(comps) == 1
    assert comps[0]["minutes"] == 300
    assert comps[0]["note"] == "reggel; délután"
    assert [i["minutes"] for i in comps[0]["items"]] == [240, 60]
    assert summary(client)["open_weighted_minutes"] == 180


def test_daily_limit_is_eight_hours(client):
    add_saturdays(client, 4)
    assert compensate(client, hours=6).status_code == 201
    response = compensate(client, hours=2.5)
    assert response.status_code == 409
    assert "legfeljebb 2 óra" in response.get_json()["error"]
    assert compensate(client, hours=2).status_code == 201
    assert compensate(client, hours=0.5).status_code == 409
    assert compensate(client, day="2026-12-02", hours=0.5).status_code == 201


def test_four_hours_at_200_percent_earn_a_day(client):
    add_saturdays(client, 1, hours=4)
    assert summary(client)["available_days"] == 1
    assert compensate(client).status_code == 201
    result = summary(client)
    assert result["open_weighted_minutes"] == 0
    assert result["compensated_weighted_minutes"] == 480
    entry = client.get("/api/entries").get_json()[0]
    assert entry["overtime_minutes"] == 240
    assert entry["compensated_weighted_minutes"] == 480


def test_available_days_follow_weighted_balance(client):
    add_saturdays(client, 2, hours=4)
    assert summary(client)["available_days"] == 2


def test_compensation_consumes_oldest_first(client):
    add_saturdays(client, 3, hours=2)  # 4 weighted hours each
    assert compensate(client).status_code == 201
    result = summary(client)
    assert result["open_weighted_minutes"] == 240
    assert result["available_days"] == 0
    entries = client.get("/api/entries").get_json()
    assert [e["compensated_weighted_minutes"] for e in entries] == [0, 240, 240]


def test_compensation_can_split_an_entry_across_rates(client):
    add(client, "2026-11-10T17:00", "2026-11-10T22:00", "A")  # 300 min at 150% = 450
    add(client, "2026-11-11T18:00", "2026-11-11T19:00", "B")  # 60 min at 150% = 90
    compensate(client)
    entries = {e["ticket"]: e for e in client.get("/api/entries").get_json()}
    assert entries["A"]["open_weighted_minutes"] == 0
    assert entries["B"]["compensated_weighted_minutes"] == 30
    assert entries["B"]["open_weighted_minutes"] == 60


def test_undo_restores_balance(client):
    add_saturdays(client, 2, hours=2)
    comp_id = compensate(client).get_json()["id"]
    assert summary(client)["open_weighted_minutes"] == 0
    assert client.delete(f"/api/compensations/{comp_id}").status_code == 204
    assert summary(client)["open_weighted_minutes"] == 480


def test_compensated_entry_cannot_be_deleted_until_undone(client):
    ids = add_saturdays(client, 2, hours=2)
    comp_id = compensate(client).get_json()["id"]
    assert client.delete(f"/api/entries/{ids[0]}").status_code == 409
    client.delete(f"/api/compensations/{comp_id}")
    assert client.delete(f"/api/entries/{ids[0]}").status_code == 204


def test_compensated_entry_cannot_shrink_below_allocation(client):
    ids = add_saturdays(client, 2, hours=2)
    compensate(client)
    response = client.put(
        f"/api/entries/{ids[0]}",
        json={"ticket": "T", "description": "x", "start": "2026-11-07T10:00", "end": "2026-11-07T11:00"},
    )
    assert response.status_code == 409
    entry = [e for e in client.get("/api/entries").get_json() if e["id"] == ids[0]][0]
    assert entry["overtime_minutes"] == 120


def test_calendar_override_changes_calculation(client):
    add(client, "2026-12-12T10:00", "2026-12-12T11:00")
    assert client.get("/api/entries").get_json()[0]["weighted_minutes"] == 120
    client.put("/api/calendar/2026-12-12", json={"kind": "work"})
    entry = client.get("/api/entries").get_json()[0]
    assert entry["overtime_minutes"] == 0
    client.delete("/api/calendar/2026-12-12")
    assert client.get("/api/entries").get_json()[0]["weighted_minutes"] == 120


def test_calendar_override_blocked_when_it_would_undo_compensation(client):
    add_saturdays(client, 2, hours=2)
    compensate(client)
    response = client.put("/api/calendar/2026-11-07", json={"kind": "work"})
    assert response.status_code == 409
    assert client.get("/api/calendar?year=2026").get_json()["overrides"] == []


def test_search_treats_wildcards_literally(client):
    add(client, "2026-11-10T18:00", "2026-11-10T19:00", ticket="ABC-1")
    assert len(client.get("/api/entries?q=abc").get_json()) == 1
    assert client.get("/api/entries?q=%25").get_json() == []


def test_csv_export(client):
    add(client, "2026-11-10T18:00", "2026-11-10T19:30", ticket="=HACK")
    body = client.get("/api/export.csv?month=2026-11").get_data(as_text=True)
    assert body.startswith("﻿Hibajegy;")
    assert "'=HACK" in body
    assert ";1,50;1,50;0,00;0,00;0,00;2,25;0,00;2,25" in body


def test_summary_always_covers_every_month(client):
    add(client, "2026-10-13T18:00", "2026-10-13T19:00", "OLD")
    add(client, "2026-11-10T18:00", "2026-11-10T19:00", "NEW")
    for url in ("/api/summary", "/api/summary?month=2026-11"):
        result = client.get(url).get_json()
        assert result["entry_count"] == 2
        assert result["overtime_minutes"] == 120
        assert result["weighted_minutes"] == 180


def test_compensation_lists_entry_details(client):
    add(client, "2026-11-07T10:00", "2026-11-07T13:00", "SAT", "hétvégi munka")
    add(client, "2026-11-10T18:00", "2026-11-10T20:00", "EVE", "esti munka")
    compensate(client)
    items = client.get("/api/compensations").get_json()[0]["items"]
    assert [i["ticket"] for i in items] == ["SAT", "EVE"]
    assert items[1]["description"] == "esti munka"
    assert items[1]["end"] == "2026-11-10T20:00"
    assert items[1]["overtime_minutes"] == 120
    assert items[1]["weighted_minutes"] == 180
    assert items[1]["minutes"] == 120
    assert items[1]["breakdown"][0]["percent"] == 150


def test_config_exposes_rates(client):
    rates = client.get("/api/config").get_json()["rates"]
    assert rates == {"evening": 150, "night": 165, "restday": 200, "restday_night": 215}


def test_pwa_files_are_served_from_root(client):
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.mimetype == "application/manifest+json"
    data = manifest.get_json(force=True)
    assert data["start_url"] == "/" and data["display"] == "standalone"
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200

    worker = client.get("/sw.js")
    assert worker.status_code == 200
    assert worker.mimetype in ("text/javascript", "application/javascript")
    assert worker.headers["Cache-Control"] == "no-cache"


def test_errors_are_json(client):
    response = client.post("/api/entries", data="not json")
    assert response.status_code == 415
    assert "error" in response.get_json()
