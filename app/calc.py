"""Overtime rules: splits a time range into pay categories.

All times are naive local wall-clock values. A minute belongs to the calendar
day it falls on, so Friday 23:00 is a weekday night and Saturday 02:00 is a
rest-day night.
"""

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache

DAY_END = 24 * 60

# (category, environment variable, default percent, label)
RATE_SETTINGS = (
    ("evening", "OVERTIME_RATE_EVENING", 150, "Hétköznap, munkaidőn kívül"),
    ("night", "OVERTIME_RATE_NIGHT", 165, "Hétköznap éjszaka"),
    ("restday", "OVERTIME_RATE_RESTDAY", 200, "Hétvége / pihenőnap"),
    ("restday_night", "OVERTIME_RATE_RESTDAY_NIGHT", 215, "Hétvége / pihenőnap éjszaka"),
)


def load_categories(env: Mapping[str, str]) -> dict[str, dict]:
    """Build the pay categories; a missing or empty variable means the default."""
    categories = {}
    for category, variable, default, label in RATE_SETTINGS:
        raw = env.get(variable) or str(default)
        try:
            percent = float(raw)
        except ValueError:
            percent = math.nan
        if not math.isfinite(percent) or percent <= 0:
            raise ValueError(
                f"{variable}={raw!r} is invalid: expected a positive percentage, e.g. {default}"
            )
        categories[category] = {
            "percent": int(percent) if percent.is_integer() else percent,
            "label": label,
        }
    return categories


CATEGORIES = load_categories(os.environ)


def _clock_minutes(env: Mapping[str, str], variable: str, default: str) -> int:
    raw = env.get(variable) or default
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", raw)
    if not match:
        raise ValueError(f"{variable}={raw!r} is invalid: expected 24-hour HH:MM, e.g. {default}")
    return int(match[1]) * 60 + int(match[2])


def load_bands(env: Mapping[str, str]) -> tuple[tuple, tuple]:
    """Return the workday and rest-day bands.

    A band is (from minute, to minute, category); None means regular working
    time. On a workday, the time outside both working hours and the night
    falls into the "evening" category, including the early morning.
    """
    night_end = _clock_minutes(env, "OVERTIME_NIGHT_END", "06:00")
    work_start = _clock_minutes(env, "OVERTIME_WORK_START", "08:30")
    work_end = _clock_minutes(env, "OVERTIME_WORK_END", "17:00")
    night_start = _clock_minutes(env, "OVERTIME_NIGHT_START", "22:00")
    if not night_end <= work_start < work_end <= night_start:
        raise ValueError(
            "Invalid schedule: expected OVERTIME_NIGHT_END <= OVERTIME_WORK_START "
            "< OVERTIME_WORK_END <= OVERTIME_NIGHT_START"
        )
    workday = (
        (0, night_end, "night"),
        (night_end, work_start, "evening"),
        (work_start, work_end, None),
        (work_end, night_start, "evening"),
        (night_start, DAY_END, "night"),
    )
    restday = (
        (0, night_end, "restday_night"),
        (night_end, night_start, "restday"),
        (night_start, DAY_END, "restday_night"),
    )
    return workday, restday


WORKDAY_BANDS, RESTDAY_BANDS = load_bands(os.environ)


def easter_sunday(year: int) -> date:
    """Anonymous Gregorian computus."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=None)
def hungarian_holidays(year: int) -> dict[date, str]:
    easter = easter_sunday(year)
    holidays = {
        date(year, 1, 1): "Újév",
        date(year, 3, 15): "Nemzeti ünnep",
        easter - timedelta(days=2): "Nagypéntek",
        easter: "Húsvétvasárnap",
        easter + timedelta(days=1): "Húsvéthétfő",
        date(year, 5, 1): "A munka ünnepe",
        easter + timedelta(days=49): "Pünkösdvasárnap",
        easter + timedelta(days=50): "Pünkösdhétfő",
        date(year, 8, 20): "Államalapítás ünnepe",
        date(year, 10, 23): "Nemzeti ünnep",
        date(year, 11, 1): "Mindenszentek",
        date(year, 12, 25): "Karácsony",
        date(year, 12, 26): "Karácsony",
    }
    return dict(sorted(holidays.items()))


def is_rest_day(day: date, overrides: dict[date, str]) -> bool:
    """overrides maps a date to 'rest' or 'work' and beats the default rule."""
    if day in overrides:
        return overrides[day] == "rest"
    return day.weekday() >= 5 or day in hungarian_holidays(day.year)


@dataclass(frozen=True)
class Segment:
    start: datetime
    end: datetime
    category: str | None

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds()) // 60


def split_segments(
    start: datetime, end: datetime, overrides: dict[date, str]
) -> list[Segment]:
    """Return the chronological pay segments covering [start, end)."""
    segments = []
    day = start.date()
    while day <= end.date():
        midnight = datetime(day.year, day.month, day.day)
        bands = RESTDAY_BANDS if is_rest_day(day, overrides) else WORKDAY_BANDS
        for band_start, band_end, category in bands:
            seg_start = max(start, midnight + timedelta(minutes=band_start))
            seg_end = min(end, midnight + timedelta(minutes=band_end))
            if seg_start < seg_end:
                segments.append(Segment(seg_start, seg_end, category))
        day += timedelta(days=1)
    return segments


def _weighted(segment_minutes: list[tuple[str, int]]) -> float:
    total = sum(CATEGORIES[cat]["percent"] * mins for cat, mins in segment_minutes)
    return round(total / 100, 2)


@dataclass(frozen=True)
class Analysis:
    total_minutes: int
    working_time_minutes: int
    overtime_minutes: int
    breakdown: dict[str, int]
    weighted_minutes: float
    compensated_weighted_minutes: float
    open_weighted_minutes: float

    def as_dict(self) -> dict:
        return {
            "total_minutes": self.total_minutes,
            "working_time_minutes": self.working_time_minutes,
            "overtime_minutes": self.overtime_minutes,
            "breakdown": [
                {
                    "category": category,
                    "label": CATEGORIES[category]["label"],
                    "percent": CATEGORIES[category]["percent"],
                    "minutes": minutes,
                }
                for category, minutes in self.breakdown.items()
            ],
            "weighted_minutes": self.weighted_minutes,
            "compensated_weighted_minutes": self.compensated_weighted_minutes,
            "open_weighted_minutes": self.open_weighted_minutes,
        }


def analyze(
    start: datetime,
    end: datetime,
    overrides: dict[date, str],
    compensated_weighted_minutes: float = 0,
) -> Analysis:
    """Compute hours and multipliers for a range.

    Compensation is counted in weighted minutes, so the open balance is the
    weighted total minus what has already been compensated.
    """
    segments = split_segments(start, end, overrides)
    overtime = [(s.category, s.minutes) for s in segments if s.category]

    breakdown = {category: 0 for category in CATEGORIES}
    for category, minutes in overtime:
        breakdown[category] += minutes
    breakdown = {cat: mins for cat, mins in breakdown.items() if mins}

    weighted = _weighted(overtime)
    return Analysis(
        total_minutes=sum(s.minutes for s in segments),
        working_time_minutes=sum(s.minutes for s in segments if not s.category),
        overtime_minutes=sum(minutes for _, minutes in overtime),
        breakdown=breakdown,
        weighted_minutes=weighted,
        compensated_weighted_minutes=compensated_weighted_minutes,
        open_weighted_minutes=round(weighted - compensated_weighted_minutes, 2),
    )
