# Túlóra nyilvántartás

Webes alkalmazás a túlórák nyilvántartására és lecsúsztatására.

## Indítás

```sh
docker compose up -d --build
```

Ezután a <http://localhost:8765> címen érhető el. Az alkalmazásban nincs bejelentkezés,
ezért a portot csak megbízható hálózaton tedd elérhetővé. A port és az elérés a `docker-compose.yaml` fájlban módosítható.

Az adatok a `data/overtime.db` SQLite fájlban vannak. Mentéshez állítsd le a konténert, és másold le a `data` mappát.
Ha a felhasználód azonosítója nem 1000, indításkor add meg: `UID=$(id -u) GID=$(id -g) docker compose up -d --build`.

A Mentés fülön az összes adat letölthető egy JSON fájlba, és onnan vissza is állítható. A visszaállítás minden jelenlegi adatot lecserél, de előtte a program a mostani állapotot automatikusan elmenti a `data/backups` mappába. A szorzókat és az idősávokat a mentés nem tartalmazza, ezek a környezeti változókból jönnek.

## Számítási szabályok

| Nap | Időszak | Alapértelmezett szorzó | Környezeti változó |
| --- | --- | --- | --- |
| Munkanap | 08:30–17:00 | munkaidő, nem számít túlórának | |
| Munkanap | 17:00–22:00 és 06:00–08:30 | 150% | `OVERTIME_RATE_EVENING` |
| Munkanap | 22:00–06:00 | 165% | `OVERTIME_RATE_NIGHT` |
| Hétvége, pihenőnap | 06:00–22:00 | 200% | `OVERTIME_RATE_RESTDAY` |
| Hétvége, pihenőnap | 22:00–06:00 | 215% | `OVERTIME_RATE_RESTDAY_NIGHT` |

Az idősávok határai is állíthatók, 24 órás `ÓÓ:PP` formában: `OVERTIME_NIGHT_END` (06:00), `OVERTIME_WORK_START` (08:30), `OVERTIME_WORK_END` (17:00), `OVERTIME_NIGHT_START` (22:00). Ezeknek ebben a sorrendben kell követniük egymást.

A szorzókat százalékban, pozitív számként kell megadni, tizedesponttal (például `137.5`). A Compose a rendszer környezeti változóit és a projektmappában lévő `.env` fájlt is figyelembe veszi. Módosítás után `docker compose up -d` kell. Hibás érték esetén a konténer nem indul el, az ok a naplóban látszik.

A szorzó és az idősáv nincs bejegyzésenként eltárolva, ezért a módosítás a korábbi bejegyzések számítását is megváltoztatja.

- Minden perc azt a szorzót kapja, amilyen naptári napra esik. A péntek 23:00 munkanapi éjszaka, a szombat 01:00 pihenőnapi éjszaka.
- A hétvégéket és a magyar törvényes ünnepeket a program magától ismeri. Az áthelyezett munkanapokat és egyéb pihenőnapokat a Naptár fülön kell megadni.
- Az időpontok falióra szerint számítanak, az óraátállítást a program nem kezeli.

## Lecsúsztatás

A nyitott szorzós órákból fél órás lépésekben, napi legfeljebb 8 óra csúsztatható le, így például 4 óra 200%-os munka egy teljes 8 órás napot fedez. Ha ugyanarra a napra többször rögzítesz, az órák egy tételben összeadódnak. A lecsúsztatás a legrégebbi nyitott túlórákból von le, szükség esetén egy bejegyzést megosztva. A lecsúsztatás visszavonható. Amíg egy bejegyzés lecsúsztatásban szerepel, nem törölhető.

## Tesztek

```sh
docker run --rm -v "$PWD":/w:ro -w /w -e PYTHONDONTWRITEBYTECODE=1 python:3.13-slim \
  sh -c "pip install -q -r requirements.txt pytest && python -m pytest -q -p no:cacheprovider"
```
