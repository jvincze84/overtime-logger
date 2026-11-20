import csv
import io
import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, Response, g, jsonify, request
from werkzeug.exceptions import HTTPException

from .calc import CATEGORIES, analyze, hungarian_holidays

COMPENSATION_MAX_MINUTES = 8 * 60
COMPENSATION_STEP_MINUTES = 30
MAX_ENTRY_DURATION = timedelta(days=7)
MIN_YEAR, MAX_YEAR = 2000, 2100
DATETIME_FORMAT = "%Y-%m-%dT%H:%M"
DATE_FORMAT = "%Y-%m-%d"

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    ticket TEXT NOT NULL,
    description TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_start ON entries(start);
CREATE TABLE IF NOT EXISTS compensations (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL UNIQUE,
    minutes INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS compensation_items (
    compensation_id INTEGER NOT NULL REFERENCES compensations(id) ON DELETE CASCADE,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE RESTRICT,
    minutes REAL NOT NULL CHECK (minutes > 0), -- weighted minutes
    PRIMARY KEY (compensation_id, entry_id)
);
CREATE INDEX IF NOT EXISTS idx_items_entry ON compensation_items(entry_id);
CREATE TABLE IF NOT EXISTS calendar_overrides (
    day TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('rest', 'work')),
    note TEXT NOT NULL DEFAULT ''
);
"""


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def db() -> sqlite3.Connection:
    return g.db


@contextmanager
def transaction():
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def json_body() -> dict:
    body = request.get_json()
    if not isinstance(body, dict):
        raise ApiError(400, "Érvénytelen kérés.")
    return body


def check_year(year: int) -> None:
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise ApiError(400, f"Az évnek {MIN_YEAR} és {MAX_YEAR} közé kell esnie.")


def parse_datetime(value, field: str) -> datetime:
    try:
        parsed = datetime.strptime(value, DATETIME_FORMAT)
    except (TypeError, ValueError):
        raise ApiError(400, f"Hiányzó vagy hibás időpont: {field}.") from None
    check_year(parsed.year)
    return parsed


def parse_date(value, field: str) -> date:
    try:
        parsed = datetime.strptime(value, DATE_FORMAT).date()
    except (TypeError, ValueError):
        raise ApiError(400, f"Hiányzó vagy hibás dátum: {field}.") from None
    check_year(parsed.year)
    return parsed


def text_field(body: dict, key: str, label: str, limit: int, required=True) -> str:
    value = body.get(key, "")
    if not isinstance(value, str):
        raise ApiError(400, f"Hibás érték: {label}.")
    value = value.strip()
    if required and not value:
        raise ApiError(400, f"Kötelező mező: {label}.")
    if len(value) > limit:
        raise ApiError(400, f"Túl hosszú mező: {label} (legfeljebb {limit} karakter).")
    return value


def parse_compensation_minutes(value) -> int:
    minutes = value * 60 if type(value) in (int, float) else math.nan
    if minutes % COMPENSATION_STEP_MINUTES != 0 or not (
        COMPENSATION_STEP_MINUTES <= minutes <= COMPENSATION_MAX_MINUTES
    ):
        raise ApiError(400, "A lecsúsztatás fél órás lépésekben adható meg, 0,5 és 8 óra között.")
    return int(minutes)


def format_plain_hours(minutes: int) -> str:
    return f"{minutes / 60:g}".replace(".", ",")


def parse_range(body: dict) -> tuple[datetime, datetime]:
    start = parse_datetime(body.get("start"), "kezdés")
    end = parse_datetime(body.get("end"), "vége")
    if end <= start:
        raise ApiError(400, "A befejezésnek a kezdés után kell lennie.")
    if end - start > MAX_ENTRY_DURATION:
        raise ApiError(400, "Egy bejegyzés legfeljebb 7 napot fedhet le.")
    return start, end


def load_overrides() -> dict[date, str]:
    rows = db().execute("SELECT day, kind FROM calendar_overrides")
    return {date.fromisoformat(r["day"]): r["kind"] for r in rows}


def load_compensated() -> dict[int, float]:
    rows = db().execute(
        "SELECT entry_id, SUM(minutes) AS total FROM compensation_items GROUP BY entry_id"
    )
    return {r["entry_id"]: round(r["total"], 2) for r in rows}


def analyze_row(row, overrides, compensated):
    return analyze(
        datetime.strptime(row["start"], DATETIME_FORMAT),
        datetime.strptime(row["end"], DATETIME_FORMAT),
        overrides,
        compensated.get(row["id"], 0),
    )


def entry_view(row, analysis) -> dict:
    return {
        "id": row["id"],
        "ticket": row["ticket"],
        "description": row["description"],
        "start": row["start"],
        "end": row["end"],
        **analysis.as_dict(),
    }


def month_filter() -> str | None:
    month = request.args.get("month") or None
    if month is not None:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise ApiError(400, "Hibás hónap.")
    return month


def select_entries(month: str | None, search: str = "") -> list[sqlite3.Row]:
    sql = "SELECT * FROM entries WHERE 1=1"
    params: list[str] = []
    if month:
        sql += " AND substr(start, 1, 7) = ?"
        params.append(month)
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql += " AND (ticket LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')"
        params += [f"%{escaped}%", f"%{escaped}%"]
    return db().execute(sql + " ORDER BY start DESC", params).fetchall()


def check_overlap(start: datetime, end: datetime, ignore_id: int | None) -> None:
    clash = db().execute(
        "SELECT ticket, start, end FROM entries WHERE start < ? AND end > ? AND id IS NOT ?",
        (end.strftime(DATETIME_FORMAT), start.strftime(DATETIME_FORMAT), ignore_id),
    ).fetchone()
    if clash:
        raise ApiError(
            409,
            "Átfedés egy meglévő bejegyzéssel: "
            f"{clash['ticket']} ({clash['start'].replace('T', ' ')} – "
            f"{clash['end'].replace('T', ' ')}).",
        )


def check_allocations_still_fit() -> None:
    """Call inside a transaction, after a change that can shrink overtime."""
    overrides = load_overrides()
    compensated = load_compensated()
    if not compensated:
        return
    marks = ",".join("?" * len(compensated))
    rows = db().execute(
        f"SELECT * FROM entries WHERE id IN ({marks})", list(compensated)
    )
    for row in rows:
        if analyze_row(row, overrides, compensated).open_weighted_minutes < 0:
            raise ApiError(
                409,
                f"A(z) {row['ticket']} bejegyzésből már több szorzós óra van lecsúsztatva, "
                "mint amennyi a módosítás után maradna. "
                "Előbb vond vissza az érintett lecsúsztatást.",
            )


def format_hours(minutes: float) -> str:
    return f"{minutes / 60:.2f}".replace(".", ",")


def csv_safe(value: str) -> str:
    return f"'{value}" if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


BACKUP_FORMAT = "overtime-backup"
BACKUP_VERSION = 1
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def build_backup() -> dict:
    conn = db()
    items: dict[int, list[dict]] = {}
    for row in conn.execute(
        "SELECT compensation_id, entry_id, minutes FROM compensation_items "
        "ORDER BY compensation_id, entry_id"
    ):
        items.setdefault(row["compensation_id"], []).append(
            {"entry_id": row["entry_id"], "minutes": row["minutes"]}
        )
    return {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        # Informational only: rates come from the environment and are not restored.
        "rates": {k: c["percent"] for k, c in CATEGORIES.items()},
        "entries": [
            dict(r)
            for r in conn.execute(
                "SELECT id, ticket, description, start, end FROM entries ORDER BY start"
            )
        ],
        "compensations": [
            {
                "id": c["id"],
                "day": c["day"],
                "minutes": c["minutes"],
                "note": c["note"],
                "items": items.get(c["id"], []),
            }
            for c in conn.execute("SELECT * FROM compensations ORDER BY day")
        ],
        "calendar_overrides": [
            dict(r)
            for r in conn.execute("SELECT day, kind, note FROM calendar_overrides ORDER BY day")
        ],
    }


def backup_json(backup: dict) -> str:
    return json.dumps(backup, ensure_ascii=False, indent=2)


def _backup_list(data: dict, key: str) -> list:
    value = data.get(key)
    if not isinstance(value, list):
        raise ApiError(400, f"Hibás mentés: hiányzik a(z) „{key}” lista.")
    return value


def _backup_records(data: dict, key: str, label: str):
    """Yield (record, error prefix) pairs, turning item errors into readable ones."""
    for number, record in enumerate(_backup_list(data, key), 1):
        if not isinstance(record, dict):
            raise ApiError(400, f"Hibás mentés, {number}. {label}: érvénytelen tétel.")
        yield record, f"Hibás mentés, {number}. {label}"


def _positive_id(value) -> int:
    if type(value) is not int or value <= 0:
        raise ApiError(400, "hibás azonosító.")
    return value


def validate_backup(data) -> tuple[list, list, list]:
    if not isinstance(data, dict) or data.get("format") != BACKUP_FORMAT:
        raise ApiError(400, "Ez a fájl nem a túlóra-nyilvántartó mentése.")
    if data.get("version") != BACKUP_VERSION:
        raise ApiError(400, f"Nem támogatott mentésverzió: {data.get('version')!r}.")

    entries = []
    for record, where in _backup_records(data, "entries", "bejegyzés"):
        try:
            entry_id = _positive_id(record.get("id"))
            if any(e[0] == entry_id for e in entries):
                raise ApiError(400, "ismétlődő azonosító.")
            ticket = text_field(record, "ticket", "Hibajegy", 100)
            description = text_field(record, "description", "Leírás", 2000)
            start, end = parse_range(record)
        except ApiError as error:
            raise ApiError(400, f"{where}: {error.message}") from None
        entries.append((entry_id, ticket, description, start, end))
    ordered = sorted(entries, key=lambda e: e[3])
    for previous, current in zip(ordered, ordered[1:]):
        if current[3] < previous[4]:
            raise ApiError(
                400, f"Hibás mentés: átfedő bejegyzések: {previous[1]} és {current[1]}."
            )

    overrides = []
    for record, where in _backup_records(data, "calendar_overrides", "egyedi nap"):
        try:
            day = parse_date(record.get("day"), "nap")
            if record.get("kind") not in ("rest", "work"):
                raise ApiError(400, "hibás naptípus.")
            note = text_field(record, "note", "Megjegyzés", 200, required=False)
        except ApiError as error:
            raise ApiError(400, f"{where}: {error.message}") from None
        if any(o[0] == day for o in overrides):
            raise ApiError(400, f"{where}: ismétlődő nap.")
        overrides.append((day, record["kind"], note))

    entry_ids = {e[0] for e in entries}
    compensations = []
    for record, where in _backup_records(data, "compensations", "lecsúsztatás"):
        try:
            comp_id = _positive_id(record.get("id"))
            day = parse_date(record.get("day"), "nap")
            minutes = record.get("minutes")
            if (
                type(minutes) is not int
                or minutes % COMPENSATION_STEP_MINUTES
                or not COMPENSATION_STEP_MINUTES <= minutes <= COMPENSATION_MAX_MINUTES
            ):
                raise ApiError(400, "hibás óraszám.")
            note = text_field(record, "note", "Megjegyzés", 5000, required=False)
            items = record.get("items")
            if not isinstance(items, list) or not items:
                raise ApiError(400, "hiányoznak a tételek.")
            parsed_items = []
            for item in items:
                if (
                    not isinstance(item, dict)
                    or type(item.get("entry_id")) is not int
                    or item["entry_id"] not in entry_ids
                ):
                    raise ApiError(400, "nem létező bejegyzésre hivatkozik.")
                if any(i[0] == item["entry_id"] for i in parsed_items):
                    raise ApiError(400, "ismétlődő tétel.")
                item_minutes = item.get("minutes")
                if type(item_minutes) not in (int, float) or not (
                    math.isfinite(item_minutes) and item_minutes > 0
                ):
                    raise ApiError(400, "hibás tétel óraszám.")
                parsed_items.append((item["entry_id"], round(item_minutes, 2)))
            if abs(sum(m for _, m in parsed_items) - minutes) > 0.05:
                raise ApiError(400, "a tételek összege nem egyezik a lecsúsztatott órákkal.")
        except ApiError as error:
            raise ApiError(400, f"{where}: {error.message}") from None
        if any(c[0] == comp_id for c in compensations):
            raise ApiError(400, f"{where}: ismétlődő azonosító.")
        if any(c[1] == day for c in compensations):
            raise ApiError(400, f"{where}: ismétlődő nap.")
        compensations.append((comp_id, day, minutes, note, parsed_items))

    return entries, overrides, compensations


def write_snapshot(folder: Path) -> str:
    """Save the current data next to the database before it gets replaced."""
    folder.mkdir(exist_ok=True)
    stem = f"import-elotti-{datetime.now():%Y%m%d-%H%M%S}"
    path = folder / f"{stem}.json"
    counter = 2
    while path.exists():
        path = folder / f"{stem}-{counter}.json"
        counter += 1
    path.write_text(backup_json(build_backup()), encoding="utf-8")
    return path.name


def register_routes(app: Flask) -> None:
    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/health")
    def health():
        db().execute("SELECT 1")
        return jsonify(status="ok")

    @app.get("/api/config")
    def config():
        return jsonify(rates={k: c["percent"] for k, c in CATEGORIES.items()})

    @app.get("/api/entries")
    def list_entries():
        overrides = load_overrides()
        compensated = load_compensated()
        search = request.args.get("q", "").strip()
        rows = select_entries(month_filter(), search)
        return jsonify(
            [entry_view(r, analyze_row(r, overrides, compensated)) for r in rows]
        )

    @app.post("/api/preview")
    def preview():
        start, end = parse_range(json_body())
        return jsonify(analyze(start, end, load_overrides()).as_dict())

    def read_entry_body():
        body = json_body()
        ticket = text_field(body, "ticket", "Hibajegy", 100)
        description = text_field(body, "description", "Leírás", 2000)
        start, end = parse_range(body)
        return ticket, description, start, end

    @app.post("/api/entries")
    def create_entry():
        ticket, description, start, end = read_entry_body()
        with transaction() as conn:
            check_overlap(start, end, None)
            cursor = conn.execute(
                "INSERT INTO entries (ticket, description, start, end) VALUES (?, ?, ?, ?)",
                (
                    ticket,
                    description,
                    start.strftime(DATETIME_FORMAT),
                    end.strftime(DATETIME_FORMAT),
                ),
            )
        return jsonify(id=cursor.lastrowid), 201

    @app.put("/api/entries/<int:entry_id>")
    def update_entry(entry_id: int):
        ticket, description, start, end = read_entry_body()
        with transaction() as conn:
            check_overlap(start, end, entry_id)
            cursor = conn.execute(
                "UPDATE entries SET ticket = ?, description = ?, start = ?, end = ? WHERE id = ?",
                (
                    ticket,
                    description,
                    start.strftime(DATETIME_FORMAT),
                    end.strftime(DATETIME_FORMAT),
                    entry_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ApiError(404, "A bejegyzés nem található.")
            check_allocations_still_fit()
        return jsonify(id=entry_id)

    @app.delete("/api/entries/<int:entry_id>")
    def delete_entry(entry_id: int):
        with transaction() as conn:
            used = conn.execute(
                "SELECT 1 FROM compensation_items WHERE entry_id = ? LIMIT 1", (entry_id,)
            ).fetchone()
            if used:
                raise ApiError(
                    409,
                    "Ez a bejegyzés szerepel egy lecsúsztatásban. "
                    "Törlés előtt vond vissza a lecsúsztatást.",
                )
            cursor = conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            if cursor.rowcount == 0:
                raise ApiError(404, "A bejegyzés nem található.")
        return "", 204

    @app.get("/api/summary")
    def summary():
        overrides = load_overrides()
        compensated = load_compensated()
        analyses = [
            analyze_row(row, overrides, compensated) for row in select_entries(None)
        ]
        open_weighted = round(sum(a.open_weighted_minutes for a in analyses), 2)
        return jsonify(
            compensation_max_minutes=COMPENSATION_MAX_MINUTES,
            compensation_step_minutes=COMPENSATION_STEP_MINUTES,
            entry_count=len(analyses),
            overtime_minutes=sum(a.overtime_minutes for a in analyses),
            weighted_minutes=round(sum(a.weighted_minutes for a in analyses), 2),
            open_weighted_minutes=open_weighted,
            compensated_weighted_minutes=round(
                sum(a.compensated_weighted_minutes for a in analyses), 2
            ),
            available_days=int(open_weighted // COMPENSATION_MAX_MINUTES),
        )

    @app.get("/api/compensations")
    def list_compensations():
        comps = db().execute("SELECT * FROM compensations ORDER BY day DESC").fetchall()
        items = db().execute(
            """SELECT i.compensation_id, i.minutes AS deducted,
                      e.id, e.ticket, e.description, e.start, e.end
               FROM compensation_items i JOIN entries e ON e.id = i.entry_id
               ORDER BY e.start"""
        ).fetchall()
        overrides = load_overrides()
        by_comp: dict[int, list[dict]] = {}
        for item in items:
            analysis = analyze_row(item, overrides, {}).as_dict()
            by_comp.setdefault(item["compensation_id"], []).append(
                {
                    "ticket": item["ticket"],
                    "description": item["description"],
                    "start": item["start"],
                    "end": item["end"],
                    "minutes": item["deducted"],
                    "overtime_minutes": analysis["overtime_minutes"],
                    "weighted_minutes": analysis["weighted_minutes"],
                    "breakdown": analysis["breakdown"],
                }
            )
        return jsonify(
            [
                {
                    "id": c["id"],
                    "day": c["day"],
                    "minutes": c["minutes"],
                    "note": c["note"],
                    "items": by_comp.get(c["id"], []),
                }
                for c in comps
            ]
        )

    @app.post("/api/compensations")
    def create_compensation():
        body = json_body()
        day = parse_date(body.get("day"), "nap")
        minutes = parse_compensation_minutes(body.get("hours"))
        note = text_field(body, "note", "Megjegyzés", 500, required=False)
        with transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM compensations WHERE day = ?", (day.isoformat(),)
            ).fetchone()
            already = existing["minutes"] if existing else 0
            if already + minutes > COMPENSATION_MAX_MINUTES:
                raise ApiError(
                    409,
                    f"Erre a napra már {format_plain_hours(already)} óra van lecsúsztatva, "
                    f"legfeljebb {format_plain_hours(COMPENSATION_MAX_MINUTES - already)} óra "
                    "adható még hozzá.",
                )

            overrides = load_overrides()
            compensated = load_compensated()
            rows = conn.execute("SELECT * FROM entries ORDER BY start").fetchall()

            allocations = []
            needed = float(minutes)
            for row in rows:
                if needed <= 0:
                    break
                available = analyze_row(row, overrides, compensated).open_weighted_minutes
                taken = min(available, needed)
                if taken > 0:
                    allocations.append((row["id"], taken))
                    needed = round(needed - taken, 2)
            if needed > 0:
                raise ApiError(409, "Nincs ennyi nyitott szorzós órád.")

            if existing:
                compensation_id = existing["id"]
                merged_note = "; ".join(part for part in (existing["note"], note) if part)
                conn.execute(
                    "UPDATE compensations SET minutes = ?, note = ? WHERE id = ?",
                    (already + minutes, merged_note, compensation_id),
                )
            else:
                compensation_id = conn.execute(
                    "INSERT INTO compensations (day, minutes, note) VALUES (?, ?, ?)",
                    (day.isoformat(), minutes, note),
                ).lastrowid
            conn.executemany(
                "INSERT INTO compensation_items (compensation_id, entry_id, minutes) "
                "VALUES (?, ?, ?) ON CONFLICT(compensation_id, entry_id) "
                "DO UPDATE SET minutes = ROUND(minutes + excluded.minutes, 2)",
                [(compensation_id, entry_id, m) for entry_id, m in allocations],
            )
        return jsonify(id=compensation_id), 201

    @app.delete("/api/compensations/<int:compensation_id>")
    def delete_compensation(compensation_id: int):
        with transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM compensations WHERE id = ?", (compensation_id,)
            )
            if cursor.rowcount == 0:
                raise ApiError(404, "A lecsúsztatás nem található.")
        return "", 204

    @app.get("/api/calendar")
    def calendar():
        try:
            year = int(request.args.get("year", ""))
        except ValueError:
            raise ApiError(400, "Hibás év.") from None
        check_year(year)
        overrides = db().execute(
            "SELECT day, kind, note FROM calendar_overrides "
            "WHERE substr(day, 1, 4) = ? ORDER BY day",
            (f"{year:04d}",),
        ).fetchall()
        return jsonify(
            holidays=[
                {"day": d.isoformat(), "name": name}
                for d, name in hungarian_holidays(year).items()
            ],
            overrides=[dict(o) for o in overrides],
        )

    @app.put("/api/calendar/<day_text>")
    def set_calendar_day(day_text: str):
        day = parse_date(day_text, "nap")
        body = json_body()
        kind = body.get("kind")
        if kind not in ("rest", "work"):
            raise ApiError(400, "Hibás naptípus.")
        note = text_field(body, "note", "Megjegyzés", 200, required=False)
        with transaction() as conn:
            conn.execute(
                "INSERT INTO calendar_overrides (day, kind, note) VALUES (?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET kind = excluded.kind, note = excluded.note",
                (day.isoformat(), kind, note),
            )
            check_allocations_still_fit()
        return jsonify(day=day.isoformat(), kind=kind, note=note)

    @app.delete("/api/calendar/<day_text>")
    def delete_calendar_day(day_text: str):
        day = parse_date(day_text, "nap")
        with transaction() as conn:
            conn.execute("DELETE FROM calendar_overrides WHERE day = ?", (day.isoformat(),))
            check_allocations_still_fit()
        return "", 204

    @app.get("/api/backup")
    def export_backup():
        filename = f"tulora-mentes-{date.today().isoformat()}.json"
        return Response(
            backup_json(build_backup()),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/backup")
    def import_backup():
        entries, overrides, compensations = validate_backup(request.get_json())
        snapshot = write_snapshot(Path(app.config["DB_PATH"]).parent / "backups")
        with transaction() as conn:
            for table in ("compensation_items", "compensations", "entries", "calendar_overrides"):
                conn.execute(f"DELETE FROM {table}")
            conn.executemany(
                "INSERT INTO entries (id, ticket, description, start, end) VALUES (?, ?, ?, ?, ?)",
                [
                    (i, t, d, s.strftime(DATETIME_FORMAT), e.strftime(DATETIME_FORMAT))
                    for i, t, d, s, e in entries
                ],
            )
            conn.executemany(
                "INSERT INTO calendar_overrides (day, kind, note) VALUES (?, ?, ?)",
                [(d.isoformat(), k, n) for d, k, n in overrides],
            )
            for comp_id, day, minutes, note, items in compensations:
                conn.execute(
                    "INSERT INTO compensations (id, day, minutes, note) VALUES (?, ?, ?, ?)",
                    (comp_id, day.isoformat(), minutes, note),
                )
                conn.executemany(
                    "INSERT INTO compensation_items (compensation_id, entry_id, minutes) "
                    "VALUES (?, ?, ?)",
                    [(comp_id, entry_id, m) for entry_id, m in items],
                )
            try:
                check_allocations_still_fit()
            except ApiError as error:
                raise ApiError(
                    400,
                    "Hibás mentés: valamelyik bejegyzésből több óra van lecsúsztatva, "
                    "mint a szorzós óraszáma. Lehet, hogy a szorzók vagy az idősávok "
                    f"eltérnek a mentéskori beállítástól. ({error.message.split('.')[0]}.)",
                ) from None
        return jsonify(
            entries=len(entries),
            compensations=len(compensations),
            calendar_overrides=len(overrides),
            snapshot=snapshot,
        )

    @app.get("/api/export.csv")
    def export_csv():
        month = month_filter()
        overrides = load_overrides()
        compensated = load_compensated()
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";", lineterminator="\r\n")
        writer.writerow(
            [
                "Hibajegy",
                "Leírás",
                "Kezdés",
                "Vége",
                "Túlóra (óra)",
                *[
                    f"{str(c['percent']).replace('.', ',')}% (óra)"
                    for c in CATEGORIES.values()
                ],
                "Szorzóval (óra)",
                "Lecsúsztatva (szorzós óra)",
                "Nyitott (szorzós óra)",
            ]
        )
        for row in reversed(select_entries(month)):
            a = analyze_row(row, overrides, compensated)
            writer.writerow(
                [
                    csv_safe(row["ticket"]),
                    csv_safe(row["description"]),
                    row["start"].replace("T", " "),
                    row["end"].replace("T", " "),
                    format_hours(a.overtime_minutes),
                    *[format_hours(a.breakdown.get(c, 0)) for c in CATEGORIES],
                    format_hours(a.weighted_minutes),
                    format_hours(a.compensated_weighted_minutes),
                    format_hours(a.open_weighted_minutes),
                ]
            )
        filename = f"tulora-{month or 'osszes'}.csv"
        return Response(
            "﻿" + output.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    path = db_path or os.environ.get("DB_PATH", "/data/overtime.db")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    app.config["DB_PATH"] = path
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

    conn = connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    conn.close()

    @app.before_request
    def open_db():
        g.db = connect(path)

    @app.teardown_request
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.errorhandler(ApiError)
    def handle_api_error(error: ApiError):
        return jsonify(error=error.message), error.status

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        return jsonify(error=error.description), error.code

    register_routes(app)
    return app
