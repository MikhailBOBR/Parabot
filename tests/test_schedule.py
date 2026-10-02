from dataclasses import asdict
from datetime import date, datetime, timezone

import pytest

from parabot.schedule import (
    assign_week,
    due_occurrence,
    due_summary,
    occurs,
    parse_csv,
    parse_lines,
    week_parity,
)


def row(text):
    return asdict(parse_lines(text)[0])


def test_manual_batch_and_date():
    result = parse_lines("Пн | 9:00-10:30 | Матан | 304\n2026-10-05 | 15:00-16:30 | Консультация")
    assert result[0].start == "09:00"
    assert result[0].day == 0
    assert result[1].on_date == "2026-10-05"
    assert occurs(asdict(result[1]), date(2026, 10, 5))
    assert not occurs(asdict(result[1]), date(2026, 10, 12))


@pytest.mark.parametrize(
    "text",
    [
        "Пн | 25:00-26:00 | Матан",
        "Пн | 9:0-10:30 | Матан",
        "Пн | 10:30-9:00 | Матан",
        "Пн | 9:00-9:00 | Матан",
        "Стр | 9:00-10:00 | Матан",
        "Пн | 9:00-10:00 |",
        "Пн | 9:00-10:00 | Матан || третья",
        "2026-02-30 | 9:00-10:00 | Матан",
        "2026-10-05 | 9:00-10:00 | Матан || odd",
        "",
        "Пн 9:00 Матан",
    ],
)
def test_invalid_input_is_rejected(text):
    with pytest.raises(ValueError):
        parse_lines(text)


def test_csv_bom_semicolon_and_quotes():
    data = '\ufeffday;start;end;name;room;week\nПн;9:00;10:30;"Матан; семинар";304;every'.encode()
    assert parse_csv(data)[0].name == "Матан; семинар"
    assert parse_csv(b"day,start,end,name\nMon,09:00,10:30,Math")[0].day == 0


@pytest.mark.parametrize(
    "data",
    [
        b"bad,headers\n1,2",
        b"\xff",
        b"x" * 100001,
        b"day,start,end,name\nMon,09:00",
        b"day,start,end,name\nMon,09:00,10:30,Math,extra",
    ],
    ids=["headers", "encoding", "oversized", "missing-column", "extra-column"],
)
def test_invalid_csv_does_not_import(data):
    with pytest.raises(ValueError):
        parse_csv(data)


def test_iso_parity_not_semester_parity():
    lesson = row("Пн | 9:00-10:30 | Матан || нечетная")
    assert date(2026, 10, 5).isocalendar().week == 41
    assert occurs(lesson, date(2026, 10, 5))
    assert not occurs(lesson, date(2026, 10, 12))


def test_due_timezone_and_grace():
    lesson = row("Чт | 9:00-10:30 | Матан")
    at = datetime(2026, 10, 1, 5, 55, tzinfo=timezone.utc)
    assert due_occurrence(lesson, at, "Europe/Moscow", 5)[0] == date(2026, 10, 1)
    assert due_occurrence(lesson, at.replace(minute=56, second=29), "Europe/Moscow", 5)
    assert not due_occurrence(lesson, at.replace(minute=57), "Europe/Moscow", 5)
    assert not due_occurrence(lesson, at.replace(minute=54), "Europe/Moscow", 5)
    assert not due_occurrence(lesson, at.replace(hour=6, minute=0), "Europe/Moscow", 5)


def test_midnight_class_reminded_previous_day():
    lesson = row("Пт | 00:02-01:00 | Ночная консультация")
    at = datetime(2026, 10, 1, 20, 57, tzinfo=timezone.utc)
    occurrence = due_occurrence(lesson, at, "Europe/Moscow", 5)
    assert occurrence[0] == date(2026, 10, 2)
    assert occurrence[1].hour == 0


def test_duplicate_batch_rejected():
    with pytest.raises(ValueError):
        parse_lines("Пн | 09:00-10:30 | Матан\nПн | 9:00-10:30 | Матан")


@pytest.mark.parametrize("week", ["odd", "even", "every"])
def test_selected_week_is_applied_to_all_text_and_csv_rows(week):
    text = parse_lines("Пн | 09:00-10:30 | Матан | 304\nВт | 11:00-12:30 | Физика")
    csv = parse_csv(b"day,start,end,name\nMon,09:00,10:30,Math")
    assert {item.week for item in assign_week(text, week)} == {week}
    assert assign_week(csv, week)[0].week == week
    assert text[0].week == "every"  # the parsed source is immutable


@pytest.mark.parametrize(
    "text,week",
    [
        ("2026-10-05 | 09:00-10:30 | Разовая", "odd"),
        ("Пн | 09:00-10:30 | Матан || четная", "odd"),
        ("Пн | 09:00-10:30 | Матан || нечетная", "every"),
        ("Пн | 09:00-10:30 | Матан\nПн | 09:00-10:30 | Матан || нечетная", "odd"),
    ],
)
def test_week_template_rejects_dates_conflicts_and_normalized_duplicates(text, week):
    with pytest.raises(ValueError):
        assign_week(parse_lines(text), week)


@pytest.mark.parametrize(
    "day,expected",
    [
        (date(2026, 9, 27), "even"),
        (date(2026, 9, 28), "odd"),
        (date(2026, 10, 4), "odd"),
        (date(2026, 10, 5), "even"),
        (date(2026, 10, 12), "odd"),
    ],
)
def test_academic_cycle_switches_on_monday_in_both_directions(day, expected):
    group = {"week_anchor": "2026-09-28", "week_anchor_parity": "odd"}
    assert week_parity(day, group) == expected


def test_academic_cycle_alternates_across_iso_53_to_iso_1():
    last, first = date(2026, 12, 28), date(2027, 1, 4)
    assert last.isocalendar().week == 53
    assert first.isocalendar().week == 1
    group = {"week_anchor": last.isoformat(), "week_anchor_parity": "odd"}
    assert week_parity(last, group) == "odd"
    assert week_parity(first, group) == "even"
    assert occurs(row("Пн | 09:00-10:30 | Матан || четная"), first, group)
    assert occurs(row("2027-01-04 | 11:00-12:30 | Разовая"), first, group)


def test_daily_summary_uses_local_time_and_a_bounded_sending_window():
    group = {"tomorrow_enabled": 1, "tomorrow_time": "23:30", "timezone": "Europe/Moscow"}
    due = datetime(2026, 10, 1, 20, 30, tzinfo=timezone.utc)
    assert due_summary(group, due.replace(minute=29, second=59)) is None
    assert due_summary(group, due) == date(2026, 10, 2)
    assert due_summary(group, due.replace(minute=31, second=30)) == date(2026, 10, 2)
    assert due_summary(group, due.replace(minute=31, second=31)) is None
    assert due_summary({**group, "tomorrow_enabled": 0}, due) is None


def test_daily_summary_never_sends_yesterdays_tomorrow_after_midnight():
    group = {"tomorrow_enabled": 1, "tomorrow_time": "23:59", "timezone": "Europe/Moscow"}
    assert due_summary(group, datetime(2026, 10, 1, 20, 59, 45, tzinfo=timezone.utc)) == date(2026, 10, 2)
    assert due_summary(group, datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc)) is None
    group["tomorrow_time"] = "00:00"
    assert due_summary(group, datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc)) == date(2026, 10, 3)
