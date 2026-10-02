"""Weekly templates and a configurable academic week cycle."""

from datetime import timedelta
from html import escape

from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup as Keyboard

from .admin_schedule import stamp, unstamp
from .messages import lesson_label
from .schedule import week_parity

PAGE = 8
WEEK_NAMES = {"even": "Чётная неделя", "odd": "Нечётная неделя", "every": "Каждую неделю"}
WEEK_ACTIONS = {"weeks", "weekview", "weekinput", "weekcsv", "setweek", "weekiso"}


class WeekAdmin:
    async def weeks_menu(self, update, context, notice=None):
        group = self.current_group(context)
        day = self.local_day(group)
        monday = day - timedelta(days=day.weekday())
        parity = week_parity(day, group)
        items = self.store.lessons(group["chat_id"])
        counts = {
            week: sum(not item["on_date"] and item["week"] == week for item in items) for week in WEEK_NAMES
        }
        basis = (
            "По учебному чередованию, заданному вами"
            if group["week_anchor"]
            else "По номеру календарной недели ISO"
        )
        text = (
            f"🌓 <b>Чётная / нечётная неделя · {escape(group['title'])}</b>\n\n"
            f"Сейчас: <b>{WEEK_NAMES[parity]}</b> · неделя с {monday:%d.%m.%Y}\n"
            f"{basis}.\nПереключение — автоматически в понедельник по часовому поясу группы.\n\n"
            "Внесите два расписания отдельно. Пары «Каждую неделю» добавляются к обоим. "
            "Разовые занятия остаются привязаны к своей дате.\n\n"
            f"Если в вузе другая чётность, укажите, какая неделя с {monday:%d.%m.%Y}. "
            "Дальше бот будет чередовать недели автоматически, в том числе при смене года."
        )
        if notice:
            text += "\n\n" + escape(notice)
        rows = [
            [Button(f"{WEEK_NAMES[week]} · {counts[week]} пар", callback_data=f"admin:weekview:{week}:0")]
            for week in ("even", "odd", "every")
        ]
        rows += [
            [Button("Эта неделя чётная", callback_data=f"admin:setweek:even:{stamp(monday)}")],
            [Button("Эта неделя нечётная", callback_data=f"admin:setweek:odd:{stamp(monday)}")],
        ]
        if group["week_anchor"]:
            rows.append([Button("Считать по календарю ISO", callback_data="admin:weekiso")])
        rows.append([Button("← Меню", callback_data="admin:menu")])
        await self.say(update, text, Keyboard(rows))

    async def week_view(self, update, context, week, page=0):
        if week not in WEEK_NAMES:
            return await self.weeks_menu(update, context)
        items = [
            item
            for item in self.store.lessons(self.current_group(context)["chat_id"])
            if not item["on_date"] and item["week"] == week
        ]
        page = min(max(0, page), max(0, (len(items) - 1) // PAGE))
        text = f"📚 <b>{WEEK_NAMES[week]}</b> · страница {page + 1}\n\n"
        text += (
            "\n\n".join(lesson_label(item) for item in items[page * PAGE : (page + 1) * PAGE])
            if items
            else "Пока пусто. Добавьте расписание этой недели."
        )
        if week != "every":
            text += "\n\nПары из раздела «Каждую неделю» также действуют на этой неделе."
        rows = [
            [
                Button("➕ Ввести расписание", callback_data=f"admin:weekinput:{week}", style="primary"),
                Button("📥 CSV", callback_data=f"admin:weekcsv:{week}"),
            ]
        ]
        nav = []
        if page:
            nav.append(Button("←", callback_data=f"admin:weekview:{week}:{page - 1}"))
        if (page + 1) * PAGE < len(items):
            nav.append(Button("→", callback_data=f"admin:weekview:{week}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows += [
            [
                Button("✏️ Изменить пару", callback_data="admin:editlist:0"),
                Button("🗑 Удалить пару", callback_data="admin:delete:0"),
            ],
            [Button("← Чётность недель", callback_data="admin:weeks")],
        ]
        await self.say(update, text, Keyboard(rows))

    async def week_action(self, update, context, data):
        action = data[1]
        group = self.current_group(context)
        if action == "weeks":
            return await self.weeks_menu(update, context)
        if action == "weekiso":
            self.store.set_group(group["chat_id"], week_anchor="", week_anchor_parity="odd")
            return await self.weeks_menu(
                update, context, "✅ Чётность определяется календарным номером недели ISO."
            )
        if action == "setweek":
            if data[2] not in ("odd", "even"):
                return await self.weeks_menu(update, context)
            reference = unstamp(data[3])
            self.store.set_week_cycle(group["chat_id"], reference, data[2])
            return await self.weeks_menu(
                update,
                context,
                f"✅ Неделя с {reference:%d.%m.%Y}: {WEEK_NAMES[data[2]].lower()}. Дальше чётность меняется автоматически.",
            )
        week = data[2]
        if week not in WEEK_NAMES:
            return await self.weeks_menu(update, context)
        if action == "weekview":
            return await self.week_view(update, context, week, int(data[3]))
        context.user_data["pending"] = (f"{action}_{week}", group["chat_id"])
        if action == "weekcsv":
            prompt = "Пришлите .csv до 100 КБ в UTF-8 с колонками <code>day,start,end,name,room</code>. Поле week можно не заполнять."
        else:
            prompt = "Отправьте расписание этой недели:\n\n<code>Пн | 09:00-10:30 | Математика | 304\nВт | 11:00-12:30 | Физика | 201</code>"
        await self.say(
            update,
            f"📚 <b>{WEEK_NAMES[week]}</b>\n\n{prompt}\n\nВыбранная чётность применяется ко всем строкам. "
            "Указывайте дни Пн–Вс. Пары добавятся к этому расписанию после подтверждения.\n/cancel — отмена.",
            Keyboard([[Button("← Назад", callback_data=f"admin:weekview:{week}:0")]]),
        )
