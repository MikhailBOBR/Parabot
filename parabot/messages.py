from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .schedule import DAYS, week_parity


def mention(member):
    if member["user_id"]:
        label = "@" + member["username"] if member["username"] else member["name"]
        return f'<a href="tg://user?id={member["user_id"]}">{escape(label)}</a>'
    return escape("@" + member["username"])


def notification_chunks(header, members, footer=""):
    """Bound HTML source length conservatively, never cut an HTML entity/mention."""
    chunks, current = [], header + "\n\n"
    for member in members:
        piece = mention(member) + " "
        if len(current) + len(piece) + len(footer) > 3500:
            chunks.append(current.rstrip())
            current = "👥 Продолжение списка:\n\n"
        current += piece
    chunks.append(current.rstrip() + ("\n\n" + footer if footer else ""))
    return chunks


def panel_keyboard(group, now):
    until = group["cooldown_until"]
    label = "🔒 Отмечаемся · пауза 5 мин" if until > now else "📣 Отмечаемся"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    label, callback_data="public:ping", style="primary" if until <= now else None
                )
            ],
            [
                InlineKeyboardButton("📅 Сегодня", callback_data="public:today"),
                InlineKeyboardButton("🙋 Я участник", callback_data="public:join"),
            ],
        ]
    )


def lesson_label(lesson):
    day = lesson["on_date"] or DAYS[lesson["day"]]
    suffix = {"odd": " · нечётная неделя", "even": " · чётная неделя", "every": ""}[lesson["week"]]
    room = f"\n📍 {escape(lesson['room'])}" if lesson["room"] else ""
    return f"{escape(day)} · {lesson['start']}–{lesson['end']}{suffix}\n<b>{escape(lesson['name'])}</b>{room}"


def lesson_button_text(lesson):
    day = lesson["on_date"] or ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")[lesson["day"]]
    week = {"odd": " · нечётная", "even": " · чётная", "every": ""}[lesson["week"]]
    return f"{day}{week} {lesson['start']} {lesson['name']}"[:60]


def day_text(store, group, day):
    header = f"📅 <b>{DAYS[day.weekday()]}, {day:%d.%m.%Y}</b> · {escape(group['timezone'])}"
    header += "\n🌓 " + ("Чётная неделя" if week_parity(day, group) == "even" else "Нечётная неделя")
    if store.day_cancelled(group["chat_id"], day):
        return header + "\n\n⛔ Все пары на этот день отменены администратором."
    if store.skipped(group["chat_id"], day):
        header += "\n🔕 Напоминания на этот день отключены."
    lessons = store.occurrences(group["chat_id"], day, include_cancelled=True)
    blocks = []
    for item in lessons:
        label = lesson_label(item)
        if item["cancelled"]:
            label = "⛔ Отменена\n" + label
        elif item["changed"]:
            label = "↪️ Перенесена / изменена\n" + label
        blocks.append(label)
    for change in store.changes_on(group["chat_id"], day):
        if (
            change["date"] == day.isoformat()
            and change["on_date"] != day.isoformat()
            and not change["cancelled"]
        ):
            blocks.append(
                f"↪️ <b>{escape(change['name'])}</b> — перенесена на {escape(change['on_date'])} "
                f"в {change['start']}–{change['end']}"
            )
    if not blocks:
        return header + "\n\nПар в расписании нет. Можно выдохнуть ☕"
    return header + "\n\n" + "\n\n".join(blocks)


def today_text(store, group, now=None):
    day = (now or datetime.now(ZoneInfo(group["timezone"]))).astimezone(ZoneInfo(group["timezone"])).date()
    return day_text(store, group, day)


def tomorrow_chunks(store, group, day):
    text = "🌙 <b>Расписание на завтра</b>\n" + day_text(store, group, day)
    chunks, current = [], ""
    continuation = f"📋 <b>Расписание на {day:%d.%m.%Y} · продолжение</b>"
    for block in text.split("\n\n"):
        if current and len(current) + len(block) + 2 > 3500:
            chunks.append(current)
            current = continuation
        current += ("\n\n" if current else "") + block
    if current:
        chunks.append(current)
    return chunks
