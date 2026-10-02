"""Personal administrator screens for dated exceptions and recurring edits."""

import secrets
from datetime import date, datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup as Keyboard

from .messages import lesson_button_text, lesson_label
from .schedule import DAYS, make_lesson, parse_lines

PAGE = 8
SCHEDULE_ACTIONS = {
    "days",
    "day",
    "daydate",
    "class",
    "cancelone",
    "restoreone",
    "move",
    "canceldayask",
    "canceldayok",
    "restoreday",
    "editlist",
    "editask",
    "editsave",
    "editdiscard",
}


def stamp(day):
    return day.strftime("%Y%m%d")


def unstamp(value):
    return datetime.strptime(value, "%Y%m%d").date()


class ScheduleAdmin:
    def local_day(self, group):
        return self.service.clock().astimezone(ZoneInfo(group["timezone"])).date()

    async def days_menu(self, update, context):
        group = self.current_group(context)
        today = self.local_day(group)
        rows = []
        for offset in range(7):
            day = today + timedelta(days=offset)
            label = "Сегодня" if offset == 0 else "Завтра" if offset == 1 else DAYS[day.weekday()]
            rows.append([Button(f"{label} · {day:%d.%m}", callback_data=f"admin:day:{stamp(day)}:0")])
        rows += [
            [Button("🗓 Другая дата", callback_data="admin:daydate")],
            [Button("✏️ Изменить постоянное расписание", callback_data="admin:editlist:0")],
            [Button("← Меню", callback_data="admin:menu")],
        ]
        await self.say(
            update,
            "📅 <b>Отмены и переносы</b>\n\nВыберите дату: можно отменить одну пару или весь день, "
            "перенести пару и вернуть исходное расписание. Изменения на дату не затрагивают следующие недели.",
            Keyboard(rows),
        )

    async def day_menu(self, update, context, day, page=0, notice=None):
        group = self.current_group(context)
        chat_id = group["chat_id"]
        items = self.store.occurrences(chat_id, day, include_cancelled=True)
        identities = {(item["id"], item["source_date"]) for item in items}
        # Keep moved-away instances available on the original day as well as the destination.
        for change in self.store.changes_on(chat_id, day):
            key = (change["lesson_id"], change["date"])
            if key not in identities:
                item = self.store.occurrence(chat_id, key[0], date.fromisoformat(key[1]))
                if item:
                    items.append(item)
        items.sort(key=lambda item: (item["start"], item["id"], item["source_date"]))
        page = min(max(0, page), max(0, (len(items) - 1) // PAGE))
        rows = []
        for item in items[page * PAGE : (page + 1) * PAGE]:
            icon = "⛔" if item["cancelled"] or item["day_cancelled"] else "↪️" if item["changed"] else "📚"
            target = f" → {item['on_date']}" if item["on_date"] != day.isoformat() else ""
            rows.append(
                [
                    Button(
                        f"{icon} {item['start']} {item['name']}{target}"[:60],
                        callback_data=f"admin:class:{item['id']}:{stamp(date.fromisoformat(item['source_date']))}:{stamp(day)}",
                    )
                ]
            )
        nav = []
        if page:
            nav.append(Button("←", callback_data=f"admin:day:{stamp(day)}:{page - 1}"))
        if (page + 1) * PAGE < len(items):
            nav.append(Button("→", callback_data=f"admin:day:{stamp(day)}:{page + 1}"))
        if nav:
            rows.append(nav)
        cancelled = self.store.day_cancelled(chat_id, day)
        rows += [
            [
                Button(
                    "↩️ Восстановить день" if cancelled else "⛔ Отменить все пары на день",
                    callback_data=f"admin:{'restoreday' if cancelled else 'canceldayask'}:{stamp(day)}",
                    style="success" if cancelled else "danger",
                )
            ],
            [
                Button("← День", callback_data=f"admin:day:{stamp(day - timedelta(days=1))}:0"),
                Button("День →", callback_data=f"admin:day:{stamp(day + timedelta(days=1))}:0"),
            ],
            [Button("← Выбор даты", callback_data="admin:days")],
        ]
        text = f"📅 <b>{DAYS[day.weekday()]}, {day:%d.%m.%Y}</b> · {escape(group['timezone'])}\n\n"
        text += "⛔ Все пары на эту дату отменены.\n\n" if cancelled else ""
        text += (
            f"Выберите пару для отмены или переноса. Страница {page + 1}."
            if items
            else "На эту дату пар нет."
        )
        if notice:
            text += "\n\n" + escape(notice)
        await self.say(update, text, Keyboard(rows))

    async def class_menu(self, update, context, lesson_id, source_day, view_day, notice=None):
        group = self.current_group(context)
        item = self.store.occurrence(group["chat_id"], lesson_id, source_day)
        if not item:
            return await self.day_menu(update, context, view_day, notice="Пара уже удалена или изменена.")
        suffix = f"{lesson_id}:{stamp(source_day)}:{stamp(view_day)}"
        rows = []
        if not item["cancelled"] and not item["day_cancelled"]:
            rows.append(
                [Button("⛔ Отменить эту пару", callback_data=f"admin:cancelone:{suffix}", style="danger")]
            )
        rows.append(
            [Button("↪️ Перенести / изменить время", callback_data=f"admin:move:{suffix}", style="primary")]
        )
        if item["changed"]:
            rows.append([Button("↩️ Вернуть исходную пару", callback_data=f"admin:restoreone:{suffix}")])
        rows.append([Button("← К дню", callback_data=f"admin:day:{stamp(view_day)}:0")])
        text = "📚 <b>Пара на конкретную дату</b>\n\n" + lesson_label(item)
        if item["cancelled"]:
            text += "\n\n⛔ Эта пара отменена."
        if item["day_cancelled"]:
            text += "\n\n⛔ На эту дату отменены все пары. Восстановите день, чтобы вернуть занятия."
        if item["changed"]:
            text += f"\n\nИсходная дата: {source_day:%d.%m.%Y}."
        text += "\n\nДействия касаются только этой пары на эту дату."
        if notice:
            text += "\n\n" + escape(notice)
        await self.say(update, text, Keyboard(rows))

    async def edit_list(self, update, context, page):
        items = self.store.lessons(self.current_group(context)["chat_id"])
        page = min(max(0, page), max(0, (len(items) - 1) // PAGE))
        rows = [
            [
                Button(
                    lesson_button_text(item),
                    callback_data=f"admin:editask:{item['id']}",
                )
            ]
            for item in items[page * PAGE : (page + 1) * PAGE]
        ]
        nav = []
        if page:
            nav.append(Button("←", callback_data=f"admin:editlist:{page - 1}"))
        if (page + 1) * PAGE < len(items):
            nav.append(Button("→", callback_data=f"admin:editlist:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([Button("← Меню", callback_data="admin:menu")])
        await self.say(
            update,
            "✏️ Выберите запись постоянного расписания для изменения." if items else "Пока нет пар.",
            Keyboard(rows),
        )

    async def schedule_action(self, update, context, data):
        action = data[1]
        group = self.current_group(context)
        chat_id = group["chat_id"]
        if action == "days":
            return await self.days_menu(update, context)
        if action == "day":
            return await self.day_menu(update, context, unstamp(data[2]), int(data[3]))
        if action == "daydate":
            context.user_data["pending"] = (action, chat_id)
            return await self.say(
                update,
                "Отправьте дату в формате <code>2026-10-05</code> или <code>05.10.2026</code>.\n/cancel — отмена.",
                Keyboard([[Button("← Выбор даты", callback_data="admin:days")]]),
            )
        if action in ("canceldayask", "canceldayok", "restoreday"):
            day = unstamp(data[2])
            if action == "canceldayask":
                return await self.say(
                    update,
                    f"⛔ Отменить все пары на <b>{day:%d.%m.%Y}</b>?\n\nНапоминаний на эту дату не будет. Другие даты сохранятся.",
                    Keyboard(
                        [
                            [
                                Button(
                                    "Да, отменить весь день",
                                    callback_data=f"admin:canceldayok:{stamp(day)}",
                                    style="danger",
                                )
                            ],
                            [Button("← Назад", callback_data=f"admin:day:{stamp(day)}:0")],
                        ]
                    ),
                )
            self.store.set_day_cancelled(chat_id, day, action == "canceldayok")
            notice = (
                "✅ Все пары на эту дату отменены."
                if action == "canceldayok"
                else "✅ День восстановлен. Отдельные отмены и переносы сохранены."
            )
            return await self.day_menu(update, context, day, notice=notice)
        if action == "editlist":
            return await self.edit_list(update, context, int(data[2]))
        if action == "editask":
            item = self.store.lesson(chat_id, int(data[2]))
            if not item:
                return await self.edit_list(update, context, 0)
            context.user_data["pending"] = ("edit", chat_id)
            context.user_data["schedule_input"] = {"lesson_id": item["id"], "original": item}
            return await self.say(
                update,
                "✏️ <b>Изменить запись расписания</b>\n\n"
                + lesson_label(item)
                + "\n\nОтправьте одну новую строку целиком:\n<code>Пн | 10:00-11:30 | Математика | 304 | каждая</code>"
                + "\n\nМожно изменить день, время, название, аудиторию и неделю. Для еженедельной записи изменение действует на все недели; уже созданные изменения на дату сохраняются.\n/cancel — отмена.",
                Keyboard([[Button("← Назад", callback_data="admin:editlist:0")]]),
            )
        if action in ("editsave", "editdiscard"):
            draft = context.user_data.get("schedule_draft")
            if not draft or draft["nonce"] != data[2] or draft["chat_id"] != chat_id:
                return await self.menu(
                    update, context, "Подтверждение устарело. Откройте пару и повторите изменение."
                )
            context.user_data.pop("schedule_draft", None)
            if action == "editdiscard":
                return await self.menu(update, context, "Изменение отменено.")
            current = (
                self.store.lesson(chat_id, draft["lesson_id"])
                if draft["kind"] == "edit"
                else self.store.occurrence(chat_id, draft["lesson_id"], draft["source_day"])
            )
            if current != draft["original"]:
                return await self.menu(
                    update, context, "Пара изменилась после предпросмотра. Откройте её заново."
                )
            try:
                if draft["kind"] == "edit":
                    self.store.update_lesson(chat_id, draft["lesson_id"], draft["replacement"])
                    return await self.menu(update, context, "✅ Постоянное расписание изменено.")
                replacement = draft["replacement"]
                begins = datetime.combine(
                    date.fromisoformat(replacement.on_date),
                    time.fromisoformat(replacement.start),
                    ZoneInfo(group["timezone"]),
                )
                if begins <= self.service.clock():
                    return await self.menu(
                        update, context, "Новое время уже наступило. Откройте пару и выберите будущее время."
                    )
                self.store.move_occurrence(
                    chat_id,
                    draft["lesson_id"],
                    draft["source_day"],
                    date.fromisoformat(replacement.on_date),
                    replacement.start,
                    replacement.end,
                    replacement.room,
                )
            except ValueError as exc:
                return await self.menu(update, context, str(exc))
            return await self.day_menu(
                update,
                context,
                date.fromisoformat(replacement.on_date),
                notice="✅ Пара перенесена. Напоминание придёт перед новым временем.",
            )
        lesson_id, source_day, view_day = int(data[2]), unstamp(data[3]), unstamp(data[4])
        if action == "class":
            return await self.class_menu(update, context, lesson_id, source_day, view_day)
        item = self.store.occurrence(chat_id, lesson_id, source_day)
        if not item:
            return await self.day_menu(update, context, view_day, notice="Пара уже удалена или изменена.")
        if action == "cancelone":
            self.store.cancel_occurrence(chat_id, lesson_id, source_day)
            return await self.class_menu(
                update,
                context,
                lesson_id,
                source_day,
                view_day,
                "✅ Пара отменена. Автонапоминания для неё отключены.",
            )
        if action == "restoreone":
            self.store.restore_occurrence(chat_id, lesson_id, source_day)
            return await self.day_menu(
                update,
                context,
                view_day,
                notice="✅ Возвращена исходная пара. Общая отмена дня, если есть, сохраняется.",
            )
        if action == "move":
            context.user_data["pending"] = ("move", chat_id)
            context.user_data["schedule_input"] = {
                "lesson_id": lesson_id,
                "source_day": source_day,
                "original": item,
            }
            return await self.say(
                update,
                "↪️ <b>Перенести одну пару</b>\n\n"
                + lesson_label(item)
                + "\n\nОтправьте новую дату и время:\n<code>2026-10-05 | 11:00-12:30</code>"
                + "\n\nАудитория сохранится. Чтобы изменить её:"
                + "\n<code>2026-10-05 | 11:00-12:30 | 201</code>"
                + "\n\nМожно указать только время <code>11:00-12:30</code>, тогда дата сохранится. Следующие недели сохранятся.\n/cancel — отмена.",
                Keyboard(
                    [
                        [
                            Button(
                                "← Назад",
                                callback_data=f"admin:class:{lesson_id}:{stamp(source_day)}:{stamp(view_day)}",
                            )
                        ]
                    ]
                ),
            )

    async def schedule_input(self, update, context, action, chat_id, text):
        context.user_data["chat_id"] = chat_id
        if action == "daydate":
            try:
                day = (
                    datetime.strptime(text.strip(), "%d.%m.%Y").date()
                    if "." in text
                    else date.fromisoformat(text.strip())
                )
            except ValueError as exc:
                raise ValueError("Нужна существующая дата: 2026-10-05 или 05.10.2026.") from exc
            context.user_data.pop("pending", None)
            return await self.day_menu(update, context, day)
        target = context.user_data.get("schedule_input")
        if not target:
            raise ValueError("Ввод устарел. Откройте пару заново.")
        if action == "edit":
            lessons = parse_lines(text)
            if len(lessons) != 1:
                raise ValueError("Для изменения отправьте ровно одну пару.")
            replacement = lessons[0]
        else:
            parts = [part.strip() for part in text.split("|")]
            if len(parts) == 1:
                parts = [target["original"]["on_date"], parts[0]]
            if len(parts) not in (2, 3):
                raise ValueError("Формат: 2026-10-05 | 11:00-12:30 | аудитория (необязательно).")
            try:
                day = date.fromisoformat(parts[0])
            except ValueError as exc:
                raise ValueError("Для переноса нужна конкретная дата в формате 2026-10-05.") from exc
            # Use the existing parser to validate the time range; preserve arbitrary original names/rooms.
            parsed = parse_lines(f"{day.isoformat()} | {parts[1]} | Перенос")[0]
            replacement = make_lesson(
                day.isoformat(),
                parsed.start,
                parsed.end,
                target["original"]["name"],
                parts[2] if len(parts) == 3 else target["original"]["room"],
            )
            group = self.store.group(chat_id)
            begins = datetime.combine(day, time.fromisoformat(replacement.start), ZoneInfo(group["timezone"]))
            if begins <= self.service.clock():
                raise ValueError("Новое начало пары должно быть в будущем.")
            if self.store.day_cancelled(chat_id, day):
                raise ValueError(
                    "На эту дату отменён весь день. Сначала восстановите его или выберите другую дату."
                )
        nonce = secrets.token_hex(4)
        context.user_data["schedule_draft"] = {
            **target,
            "kind": action,
            "chat_id": chat_id,
            "replacement": replacement,
            "nonce": nonce,
        }
        context.user_data.pop("pending", None)
        context.user_data.pop("schedule_input", None)
        title = (
            "Изменить запись постоянного расписания?" if action == "edit" else "Перенести только эту пару?"
        )
        await self.say(
            update,
            title
            + "\n\n<b>Было:</b>\n"
            + lesson_label(target["original"])
            + "\n\n<b>Будет:</b>\n"
            + lesson_label(replacement.__dict__),
            Keyboard(
                [
                    [
                        Button(
                            "✅ Сохранить",
                            callback_data=f"admin:editsave:{nonce}:g={chat_id}",
                            style="success",
                        ),
                        Button("Отмена", callback_data=f"admin:editdiscard:{nonce}:g={chat_id}"),
                    ]
                ]
            ),
        )
