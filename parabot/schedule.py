import csv
import io
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
ALIASES = {
    key: i
    for i, words in enumerate(
        (
            ("пн", "понедельник", "mon"),
            ("вт", "вторник", "tue"),
            ("ср", "среда", "wed"),
            ("чт", "четверг", "thu"),
            ("пт", "пятница", "fri"),
            ("сб", "суббота", "sat"),
            ("вс", "воскресенье", "sun"),
        )
    )
    for key in words
}
WEEKS = {
    "каждая": "every",
    "every": "every",
    "": "every",
    "все": "every",
    "нечетная": "odd",
    "нечётная": "odd",
    "odd": "odd",
    "четная": "even",
    "чётная": "even",
    "even": "even",
}


@dataclass(frozen=True)
class Lesson:
    day: int
    start: str
    end: str
    name: str
    room: str = ""
    week: str = "every"
    on_date: str = ""


def parse_time(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", value):
        raise ValueError("Время записывается как 09:00 или 9:00.")
    try:
        hour, minute = map(int, value.split(":"))
        return time(hour, minute).strftime("%H:%M")
    except ValueError as exc:
        raise ValueError("Часы должны быть 0–23, минуты 0–59.") from exc


def make_lesson(day, start, end, name, room="", week="every") -> Lesson:
    day = day.strip().lower()
    on_date = ""
    if day in ALIASES:
        weekday = ALIASES[day]
    else:
        try:
            d = date.fromisoformat(day)
        except ValueError as exc:
            raise ValueError("День: Пн, Вт, Ср, Чт, Пт, Сб, Вс или дата 2026-10-05.") from exc
        weekday, on_date = d.weekday(), d.isoformat()
    start, end = parse_time(start), parse_time(end)
    if end <= start:
        raise ValueError("Конец пары должен быть позже начала в тот же день.")
    name, room = name.strip(), room.strip()
    if not name or len(name) > 180:
        raise ValueError("Название пары: от 1 до 180 символов.")
    if len(room) > 240:
        raise ValueError("Аудитория / ссылка: до 240 символов.")
    week = WEEKS.get(week.strip().lower())
    if week is None:
        raise ValueError("Неделя: каждая, четная или нечетная.")
    if on_date and week != "every":
        raise ValueError("У разовой пары на конкретную дату укажите неделю «каждая».")
    return Lesson(weekday, start, end, name, room, week, on_date)


def parse_lines(text: str) -> list[Lesson]:
    lessons = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [x.strip() for x in line.split("|")]
        if not 3 <= len(parts) <= 5:
            raise ValueError(f"Строка {number}: Пн | 09:00-10:30 | Название | Аудитория | каждая")
        times = re.split(r"\s*[-–—]\s*", parts[1])
        if len(times) != 2:
            raise ValueError(f"Строка {number}: интервал времени, например 09:00-10:30.")
        try:
            lessons.append(
                make_lesson(
                    parts[0],
                    *times,
                    parts[2],
                    parts[3] if len(parts) >= 4 else "",
                    parts[4] if len(parts) == 5 else "каждая",
                )
            )
        except ValueError as exc:
            raise ValueError(f"Строка {number}: {exc}") from exc
    return validate_batch(lessons)


def parse_csv(data: bytes) -> list[Lesson]:
    if len(data) > 100_000:
        raise ValueError("Файл должен быть не больше 100 КБ.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Сохраните CSV в UTF-8. В Excel: CSV UTF-8.") from exc
    first = text.splitlines()[0] if text.splitlines() else ""
    reader = csv.DictReader(io.StringIO(text), delimiter=";" if ";" in first else ",")
    if not {"day", "start", "end", "name"}.issubset(reader.fieldnames or []):
        raise ValueError("Нужны колонки day,start,end,name. Необязательные: room,week.")
    lessons = []
    for number, row in enumerate(reader, 2):
        try:
            if None in row or any(row.get(k) is None for k in ("day", "start", "end", "name")):
                raise ValueError("Число столбцов не совпадает с заголовком.")
            lessons.append(
                make_lesson(
                    row["day"],
                    row["start"],
                    row["end"],
                    row["name"],
                    row.get("room") or "",
                    row.get("week") or "every",
                )
            )
        except ValueError as exc:
            raise ValueError(f"Строка CSV {number}: {exc}") from exc
    return validate_batch(lessons)


def validate_batch(lessons):
    if not lessons:
        raise ValueError("Не найдено ни одной пары.")
    if len(lessons) > 100:
        raise ValueError("За раз можно добавить до 100 пар.")
    if len(set(lessons)) != len(lessons):
        raise ValueError("В сообщении / файле есть одинаковые пары. Уберите дубликаты.")
    return lessons


def assign_week(lessons, week):
    """Apply the selected weekly template to a text/CSV batch."""
    if week not in ("odd", "even", "every"):
        raise ValueError("Выберите чётную, нечётную или каждую неделю.")
    if any(item.on_date for item in lessons):
        raise ValueError(
            "Для недельного расписания нужны дни Пн–Вс. Разовую дату добавьте через «Ввести пары»."
        )
    if any(item.week not in ("every", week) for item in lessons):
        raise ValueError(
            "Чётность в строке не совпадает с выбранным расписанием. Уберите поле недели или исправьте его."
        )
    return validate_batch([replace(item, week=week) for item in lessons])


def week_parity(day: date, group=None):
    """ISO by default; an academic anchor alternates continuously every Monday."""
    anchor = (group or {}).get("week_anchor", "")
    if not anchor:
        return "odd" if day.isocalendar().week % 2 else "even"
    reference = date.fromisoformat(anchor)
    monday = day - timedelta(days=day.weekday())
    reference -= timedelta(days=reference.weekday())
    elapsed = (monday - reference).days // 7
    initial = group["week_anchor_parity"]
    return initial if elapsed % 2 == 0 else "even" if initial == "odd" else "odd"


def occurs(lesson: dict, day: date, group=None) -> bool:
    if lesson["on_date"]:
        return lesson["on_date"] == day.isoformat()
    if lesson["day"] != day.weekday():
        return False
    parity = week_parity(day, group)
    return lesson["week"] in ("every", parity)


def due_occurrence(lesson: dict, now: datetime, timezone: str, before: int, grace=90):
    """Check both today and tomorrow: a 00:02 class is reminded at 23:57."""
    # A last-minute reschedule should still notify before the new start.
    if lesson.get("changed"):
        grace = max(grace, before * 60)
    local = now.astimezone(ZoneInfo(timezone))
    for offset in (0, 1):
        day = local.date() + timedelta(days=offset)
        if not occurs(lesson, day):
            continue
        begins = datetime.combine(day, time.fromisoformat(lesson["start"]), ZoneInfo(timezone))
        due = begins - timedelta(minutes=before)
        delay = (now - due).total_seconds()
        if 0 <= delay <= grace and now < begins:
            return day, begins
    return None


def due_summary(group, now: datetime, grace=90):
    """Return tomorrow's date only within today's configured sending window."""
    if not group["tomorrow_enabled"]:
        return None
    local = now.astimezone(ZoneInfo(group["timezone"]))
    due = datetime.combine(local.date(), time.fromisoformat(group["tomorrow_time"]), local.tzinfo)
    if 0 <= (now - due).total_seconds() <= grace:
        return local.date() + timedelta(days=1)
    return None
