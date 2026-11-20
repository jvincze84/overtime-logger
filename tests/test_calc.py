from datetime import date, datetime

import pytest

from app.calc import (
    analyze,
    easter_sunday,
    hungarian_holidays,
    load_bands,
    load_categories,
)


def dt(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


def run(start, end, overrides=None, compensated=0):
    return analyze(dt(start), dt(end), overrides or {}, compensated)


# 2026-11-10 is a Tuesday, 2026-11-13 Friday, 2026-11-14 Saturday.


def test_weekday_evening_is_150():
    a = run("2026-11-10 17:00", "2026-11-10 19:00")
    assert a.breakdown == {"evening": 120}
    assert a.weighted_minutes == 180


def test_weekday_night_is_165():
    a = run("2026-11-10 22:00", "2026-11-11 06:00")
    assert a.breakdown == {"night": 480}
    assert a.weighted_minutes == 480 * 1.65


def test_evening_to_night_boundary():
    a = run("2026-11-10 21:00", "2026-11-10 23:00")
    assert a.breakdown == {"evening": 60, "night": 60}


def test_working_hours_do_not_count():
    a = run("2026-11-10 09:00", "2026-11-10 16:00")
    assert a.overtime_minutes == 0
    assert a.working_time_minutes == 420
    assert a.weighted_minutes == 0


def test_partial_overlap_with_working_hours():
    a = run("2026-11-10 16:00", "2026-11-10 18:00")
    assert a.working_time_minutes == 60
    assert a.breakdown == {"evening": 60}


def test_early_morning_before_work_is_150():
    a = run("2026-11-10 07:30", "2026-11-10 09:30")
    assert a.breakdown == {"evening": 60}
    assert a.working_time_minutes == 60


def test_night_ends_at_0600_on_workdays():
    a = run("2026-11-10 05:00", "2026-11-10 07:00")
    assert a.breakdown == {"night": 60, "evening": 60}


def test_weekend_day_is_200():
    a = run("2026-11-14 10:00", "2026-11-14 12:00")
    assert a.breakdown == {"restday": 120}
    assert a.weighted_minutes == 240


def test_weekend_night_is_215():
    a = run("2026-11-14 22:00", "2026-11-15 06:00")
    assert a.breakdown == {"restday_night": 480}


def test_night_ends_at_0600_on_rest_days():
    a = run("2026-11-14 05:00", "2026-11-14 07:00")
    assert a.breakdown == {"restday_night": 60, "restday": 60}


def test_weekend_covers_0600_to_2200_at_200():
    a = run("2026-11-14 05:00", "2026-11-14 23:00")
    assert a.breakdown == {"restday_night": 120, "restday": 960}


def test_friday_night_into_saturday_splits_at_midnight():
    a = run("2026-11-13 23:00", "2026-11-14 01:00")
    assert a.breakdown == {"night": 60, "restday_night": 60}


def test_sunday_night_into_monday_splits_at_midnight():
    a = run("2026-11-15 23:00", "2026-11-16 01:00")
    assert a.breakdown == {"night": 60, "restday_night": 60}


def test_public_holiday_counts_as_rest_day():
    # 2026-10-23 is a Friday and a national holiday.
    a = run("2026-10-23 10:00", "2026-10-23 11:00")
    assert a.breakdown == {"restday": 60}


def test_override_makes_saturday_a_workday():
    a = run("2026-12-12 18:00", "2026-12-12 19:00", {date(2026, 12, 12): "work"})
    assert a.breakdown == {"evening": 60}


def test_override_makes_weekday_a_rest_day():
    a = run("2026-12-24 10:00", "2026-12-24 11:00", {date(2026, 12, 24): "rest"})
    assert a.breakdown == {"restday": 60}


def test_compensation_is_subtracted_from_weighted_minutes():
    a = run("2026-11-10 21:00", "2026-11-10 23:00", compensated=90)
    assert a.weighted_minutes == 189
    assert a.open_weighted_minutes == 99


def test_rates_default_when_unset_or_empty():
    categories = load_categories({"OVERTIME_RATE_NIGHT": ""})
    assert [c["percent"] for c in categories.values()] == [150, 165, 200, 215]


def test_rates_come_from_environment():
    categories = load_categories(
        {"OVERTIME_RATE_EVENING": "175", "OVERTIME_RATE_RESTDAY_NIGHT": "237.5"}
    )
    assert categories["evening"]["percent"] == 175
    assert categories["restday_night"]["percent"] == 237.5
    assert categories["night"]["percent"] == 165


@pytest.mark.parametrize("value", ["abc", "0", "-10", "nan", "inf", "150%"])
def test_invalid_rate_is_rejected_by_name(value):
    with pytest.raises(ValueError, match="OVERTIME_RATE_RESTDAY"):
        load_categories({"OVERTIME_RATE_RESTDAY": value})


def test_schedule_defaults():
    workday, restday = load_bands({})
    assert workday == (
        (0, 360, "night"),
        (360, 510, "evening"),
        (510, 1020, None),
        (1020, 1320, "evening"),
        (1320, 1440, "night"),
    )
    assert restday == ((0, 360, "restday_night"), (360, 1320, "restday"), (1320, 1440, "restday_night"))


def test_schedule_comes_from_environment():
    workday, _ = load_bands(
        {"OVERTIME_NIGHT_END": "05:00", "OVERTIME_WORK_START": "09:00", "OVERTIME_NIGHT_START": "21:30"}
    )
    assert workday[0] == (0, 300, "night")
    assert workday[2] == (540, 1020, None)
    assert workday[4] == (1290, 1440, "night")


@pytest.mark.parametrize("value", ["abc", "24:00", "6", "06:60", "6pm"])
def test_invalid_time_is_rejected_by_name(value):
    with pytest.raises(ValueError, match="OVERTIME_NIGHT_END"):
        load_bands({"OVERTIME_NIGHT_END": value})


def test_schedule_order_is_enforced():
    with pytest.raises(ValueError, match="Invalid schedule"):
        load_bands({"OVERTIME_NIGHT_END": "09:00"})


def test_easter_dates():
    assert easter_sunday(2026) == date(2026, 4, 5)
    assert easter_sunday(2027) == date(2027, 3, 28)
    assert easter_sunday(2024) == date(2024, 3, 31)


def test_holidays_include_movable_days():
    holidays = hungarian_holidays(2026)
    assert date(2026, 4, 3) in holidays  # Good Friday
    assert date(2026, 4, 6) in holidays  # Easter Monday
    assert date(2026, 5, 25) in holidays  # Whit Monday
