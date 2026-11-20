import json

import pytest

from app.main import create_app


@pytest.fixture
def app(tmp_path):
    return create_app(str(tmp_path / "data" / "test.db"))


@pytest.fixture
def client(app):
    return app.test_client()


def add(client, start, end, ticket="T-1"):
    response = client.post(
        "/api/entries",
        json={"ticket": ticket, "description": "munka | leírás", "start": start, "end": end},
    )
    assert response.status_code == 201
    return response.get_json()["id"]


def seed(client):
    add(client, "2026-11-07T10:00", "2026-11-07T13:00", "SAT")
    add(client, "2026-11-10T18:00", "2026-11-10T20:00", "EVE")
    client.post("/api/compensations", json={"day": "2026-11-20", "hours": 7.5, "note": "szabadnap"})
    client.put("/api/calendar/2026-12-12", json={"kind": "work", "note": "áthelyezett"})


def state(client):
    return {
        "entries": client.get("/api/entries").get_json(),
        "compensations": client.get("/api/compensations").get_json(),
        "calendar": client.get("/api/calendar?year=2026").get_json(),
        "summary": client.get("/api/summary").get_json(),
    }


def backup(client):
    response = client.get("/api/backup")
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    return json.loads(response.get_data(as_text=True))


def test_backup_round_trip_into_empty_database(client, tmp_path):
    seed(client)
    original = state(client)
    data = backup(client)
    assert data["format"] == "overtime-backup"
    assert len(data["entries"]) == 2

    other = create_app(str(tmp_path / "other" / "x.db")).test_client()
    response = other.post("/api/backup", json=data)
    assert response.status_code == 200
    assert response.get_json()["entries"] == 2
    assert state(other) == original


def test_restore_replaces_current_data_and_keeps_snapshot(client, tmp_path):
    seed(client)
    data = backup(client)
    original = state(client)

    client.delete(f"/api/compensations/{data['compensations'][0]['id']}")
    add(client, "2026-11-25T18:00", "2026-11-25T19:00", "NEW")
    client.delete("/api/calendar/2026-12-12")

    response = client.post("/api/backup", json=data)
    assert response.status_code == 200
    assert state(client) == original

    snapshot = tmp_path / "data" / "backups" / response.get_json()["snapshot"]
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert [e["ticket"] for e in saved["entries"]] == ["SAT", "EVE", "NEW"]
    assert saved["compensations"] == []


def test_restoring_saved_snapshot_undoes_import(client, tmp_path):
    seed(client)
    before = state(client)
    snapshot_name = client.post(
        "/api/backup",
        json={"format": "overtime-backup", "version": 1, "entries": [], "compensations": [], "calendar_overrides": []},
    ).get_json()["snapshot"]
    assert client.get("/api/entries").get_json() == []
    snapshot = json.loads((tmp_path / "data" / "backups" / snapshot_name).read_text(encoding="utf-8"))
    assert client.post("/api/backup", json=snapshot).status_code == 200
    assert state(client) == before


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.update(format="other"), "nem a túlóra-nyilvántartó"),
        (lambda d: d.update(version=99), "verzió"),
        (lambda d: d.pop("entries"), "entries"),
        (lambda d: d["entries"][0].update(ticket=""), "1. bejegyzés: Kötelező mező: Hibajegy"),
        (lambda d: d["entries"][1].update(id=d["entries"][0]["id"]), "ismétlődő azonosító"),
        (lambda d: d["entries"][1].update(start="2026-11-07T11:00", end="2026-11-07T12:00"), "átfedő"),
        (lambda d: d["compensations"][0]["items"][0].update(entry_id=999), "nem létező"),
        (lambda d: d["compensations"][0].update(minutes=45), "hibás óraszám"),
        (lambda d: d["compensations"][0]["items"][0].update(minutes=1), "összege"),
        (lambda d: d["calendar_overrides"][0].update(kind="x"), "naptípus"),
        # SAT is worth 360 weighted minutes, so 450 from it alone cannot fit.
        (lambda d: d["compensations"][0]["items"][0].update(minutes=450)
            or d["compensations"][0]["items"].pop(), "több óra van lecsúsztatva"),
        (lambda d: d["compensations"][0]["items"][0].update(entry_id=[1]), "nem létező"),
    ],
)
def test_invalid_backup_is_rejected_and_data_untouched(client, mutate, message):
    seed(client)
    data = backup(client)
    before = state(client)
    mutate(data)
    response = client.post("/api/backup", json=data)
    assert response.status_code == 400
    assert message in response.get_json()["error"]
    assert state(client) == before


def test_non_object_body_is_rejected(client):
    response = client.post("/api/backup", json=[1, 2])
    assert response.status_code == 400
