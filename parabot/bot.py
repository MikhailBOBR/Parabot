import csv
import io
import logging
import math
import re
import secrets
from datetime import timedelta
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import BotCommand, Update
from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup as Keyboard
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .admin_schedule import SCHEDULE_ACTIONS, ScheduleAdmin
from .admin_weeks import WEEK_ACTIONS, WEEK_NAMES, WeekAdmin
from .config import Settings
from .db import Store
from .messages import day_text, lesson_button_text, lesson_label, panel_keyboard, today_text
from .schedule import assign_week, parse_csv, parse_lines, parse_time, week_parity
from .service import Notifications

log = logging.getLogger(__name__)
GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)
PAGE = 8
HELP = """<b>ПараБот · пары без суеты ☕</b>

📣 /ping — позвать выбранных участников: «Отмечаемся!»
📅 /today — пары на сегодня
📋 /schedule — всё расписание
🙋 /join — познакомить бота с собой; получателей выбирает администратор
🔕 /leave — убрать себя из напоминаний
🆔 /id — узнать свой Telegram ID

<b>Для владельца бота</b>
/setup — подключить группу и текущую тему
/topic — сделать текущую тему местом уведомлений
/panel — опубликовать панель с кнопкой «Отмечаемся»
/admin — личное меню настройки
В меню: «Отмены и переносы» на дату и «Изменить постоянное расписание».
«Чётная / нечётная неделя» — два расписания и настройка автоматического чередования.
«Расписание на завтра» — ежедневная отправка в выбранное время.
/add — ответом на сообщение добавить человека в получатели
/cancel — отменить ввод

Кнопка «Отмечаемся» и /ping используют одну блокировку на 5 минут для всей группы. Это призыв отметиться на вашей паре; бот не отмечает посещаемость в системе вуза.
"""


class UI(ScheduleAdmin, WeekAdmin):
    def __init__(self, settings, store, service, app=None):
        self.settings, self.store, self.service = settings, store, service
        self.app = app

    def is_admin(self, user):
        return bool(user and not user.is_bot and user.id in self.settings.admin_ids)

    async def say(self, update, text, keyboard=None):
        chat = update.effective_chat
        if not chat:
            return
        if keyboard and chat.type == ChatType.PRIVATE and self.app:
            chat_id = self.app.user_data.get(update.effective_user.id, {}).get("chat_id")
            if chat_id:
                rows = []
                for row in keyboard.inline_keyboard:
                    buttons = []
                    for button in row:
                        data = button.callback_data
                        if data and data.startswith("admin:") and ":g=" not in data:
                            buttons.append(
                                Button(button.text, callback_data=data + f":g={chat_id}", style=button.style)
                            )
                        else:
                            buttons.append(button)
                    rows.append(buttons)
                keyboard = Keyboard(rows)
        # Personal menus update in place instead of filling the conversation with screens.
        if update.callback_query and chat.type == ChatType.PRIVATE and len(text) <= 3800:
            try:
                await update.callback_query.edit_message_text(
                    text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=keyboard
                )
            except BadRequest as exc:
                if "not modified" not in str(exc).lower():
                    raise
            return
        thread = update.effective_message.message_thread_id if update.effective_message else None
        if chat.type in GROUP_TYPES and thread == 1:
            thread = None
        # Keep well-formed blocks; don't truncate HTML or Telegram mentions.
        if len(text) > 3800:
            blocks, current = [], ""
            for block in text.split("\n\n"):
                if len(current) + len(block) > 3800 and current:
                    blocks.append(current)
                    current = ""
                current += ("\n\n" if current else "") + block
            if current:
                blocks.append(current)
        else:
            blocks = [text]
        for i, block in enumerate(blocks):
            await self.service.bot.send_message(
                chat_id=chat.id,
                message_thread_id=thread,
                text=block,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=keyboard if i == len(blocks) - 1 else None,
            )

    async def owner_only(self, update):
        if self.is_admin(update.effective_user) and not getattr(
            update.effective_message, "sender_chat", None
        ):
            return True
        if update.callback_query:
            await update.callback_query.answer("Настройки доступны только владельцу бота.", show_alert=True)
        else:
            await self.say(update, "Настройки доступны владельцу, чей ID указан в ADMIN_IDS. Свой ID: /id.")
        return False

    async def configured(self, update):
        chat = update.effective_chat
        if not chat or chat.type not in GROUP_TYPES:
            await self.say(update, "Эта команда работает в учебной группе. Для настройки в личке: /admin.")
            return None
        group = self.store.group(chat.id)
        if not group:
            await self.say(update, "Группа ещё не подключена. Владельцу бота: /setup в нужной теме.")
        return group

    async def start(self, update, context):
        if update.effective_chat.type == ChatType.PRIVATE and self.is_admin(update.effective_user):
            if context.args and context.args[0].startswith("manage_"):
                try:
                    chat_id = -int(context.args[0][7:])
                except ValueError:
                    chat_id = 0
                if self.store.group(chat_id):
                    context.user_data["chat_id"] = chat_id
                    return await self.menu(update, context)
            return await self.groups_menu(update)
        await self.say(update, HELP)

    async def help(self, update, context):
        await self.say(update, HELP)

    async def show_id(self, update, context):
        user = update.effective_user
        if user:
            text = f"Ваш ID: <code>{user.id}</code>"
            if update.effective_chat.type in GROUP_TYPES:
                text += f"\nГруппа: <code>{update.effective_chat.id}</code>\nТема: <code>{update.effective_message.message_thread_id or 'общая'}</code>"
            await self.say(update, text)

    async def setup(self, update, context):
        if not await self.owner_only(update):
            return
        chat = update.effective_chat
        if chat.type not in GROUP_TYPES:
            return await self.say(update, "Добавьте бота в группу и отправьте /setup в теме для уведомлений.")
        member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
        if member.status not in ("administrator", "creator"):
            return await self.say(
                update, "Подключить группу может владелец бота, который является администратором этой группы."
            )
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
        if me.status != "administrator":
            return await self.say(
                update,
                "Сначала назначьте бота администратором группы. Это нужно для проверки участников и получения событий входа / выхода.",
            )
        thread = update.effective_message.message_thread_id
        # General forum topic can be represented as 1; omit it when sending there.
        thread = None if thread in (None, 1) else thread
        self.store.setup_group(chat.id, chat.title or "Учебная группа", thread, self.settings.timezone)
        self.store.observe(
            chat.id, update.effective_user.id, update.effective_user.username, update.effective_user.full_name
        )
        link = f"https://t.me/{context.bot.username}?start=manage_{abs(chat.id)}"
        await self.say(
            update,
            "✅ Группа подключена. Напоминания и вызовы пойдут в эту тему.\n\nОткройте меню, выберите получателей, внесите расписание и опубликуйте /panel.",
            Keyboard([[Button("⚙️ Настроить в личке", url=link)]]),
        )

    async def set_topic(self, update, context):
        if not await self.owner_only(update):
            return
        group = await self.configured(update)
        if not group:
            return
        thread = update.effective_message.message_thread_id
        self.store.set_group(group["chat_id"], thread_id=None if thread in (None, 1) else thread)
        await self.say(
            update, "✅ Уведомления направлены в эту тему. Командой /panel можно разместить здесь кнопку."
        )

    async def publish_panel(self, chat_id):
        group = self.store.group(chat_id)
        msg = await self.service.bot.send_message(
            chat_id=chat_id,
            message_thread_id=group["thread_id"],
            text="☕ <b>ПараБот · панель группы</b>\n\nНужно позвать всех из выбранного списка? Нажмите «Отмечаемся».\nПовторный вызов доступен через 5 минут.\n\n«Я участник» знакомит бота с вами; список получателей настраивает администратор.",
            parse_mode=ParseMode.HTML,
            reply_markup=panel_keyboard(group, self.service.clock().timestamp()),
        )
        self.store.set_group(chat_id, panel_id=msg.message_id, panel_thread=group["thread_id"])
        return msg

    async def panel(self, update, context):
        if not await self.owner_only(update):
            return
        group = await self.configured(update)
        if group:
            await self.publish_panel(group["chat_id"])

    async def join(self, update, context):
        group = await self.configured(update)
        if group and update.effective_user and not update.effective_user.is_bot:
            u = update.effective_user
            self.store.observe(group["chat_id"], u.id, u.username, u.full_name)
            self.store.mute(group["chat_id"], u.id, False)
            await self.say(
                update,
                "🙋 Знакомы! Теперь администратор может выбрать вас в получатели. Работает и без @username.",
            )

    async def leave(self, update, context):
        group = await self.configured(update)
        if group and update.effective_user:
            self.store.mute(group["chat_id"], update.effective_user.id, True)
            await self.say(update, "🔕 Вас убрали из напоминаний. Вернуться в доступные для выбора: /join.")

    async def add(self, update, context):
        if not await self.owner_only(update):
            return
        group = await self.configured(update)
        if not group:
            return
        reply = update.effective_message.reply_to_message
        if not reply or not reply.from_user or reply.from_user.is_bot or reply.sender_chat:
            return await self.say(update, "Отправьте /add ответом на обычное сообщение нужного человека.")
        user = reply.from_user
        member = await context.bot.get_chat_member(group["chat_id"], user.id)
        if not self.member_present(member):
            return await self.say(update, "Этот человек уже не состоит в группе.")
        member_id = self.store.observe(group["chat_id"], user.id, user.username, user.full_name)
        self.store.select(group["chat_id"], member_id, True)
        await self.say(update, f"✅ {escape(user.full_name)} добавлен(а) в получатели.")

    async def ping(self, update, context):
        group = await self.configured(update)
        if group:
            if (
                not update.effective_user
                or update.effective_user.is_bot
                or update.effective_message.sender_chat
            ):
                return await self.say(update, "Нажмите кнопку или отправьте команду от личного аккаунта.")
            result = await self.service.ping(group["chat_id"], update.effective_user.full_name)
            await self.say(update, result)

    async def today(self, update, context):
        group = await self.configured(update)
        if group:
            await self.say(update, today_text(self.store, group, self.service.clock()))

    async def schedule(self, update, context):
        group = await self.configured(update)
        if group:
            await self.say(update, self.schedule_text(group["chat_id"]))

    def schedule_text(self, chat_id):
        lessons = self.store.lessons(chat_id)
        group = self.store.group(chat_id)
        header = f"📋 <b>Постоянное расписание</b> · {escape(group['timezone'])}\nИзменения на дату учитываются в /today."
        header += f"\n🌓 Сейчас: {WEEK_NAMES[week_parity(self.local_day(group), group)].lower()}"
        return (
            header
            + "\n\n"
            + (
                "\n\n".join(lesson_label(x) for x in lessons)
                if lessons
                else "Пока пусто. Добавьте пары через /admin."
            )
        )

    async def admin(self, update, context):
        if not await self.owner_only(update):
            return
        if update.effective_chat.type != ChatType.PRIVATE:
            link = f"https://t.me/{context.bot.username}?start=manage_{abs(update.effective_chat.id)}"
            return await self.say(
                update, "Управление — в личном меню бота.", Keyboard([[Button("⚙️ Открыть меню", url=link)]])
            )
        context.user_data.pop("pending", None)
        context.user_data.pop("draft", None)
        context.user_data.pop("schedule_draft", None)
        context.user_data.pop("schedule_input", None)
        await self.groups_menu(update)

    async def groups_menu(self, update):
        groups = self.store.groups()
        buttons = [[Button(g["title"][:60], callback_data=f"group:{g['chat_id']}")] for g in groups]
        text = (
            "⚙️ <b>Ваши группы</b>\nВыберите группу для настройки."
            if groups
            else "Пока нет групп. Добавьте бота в группу, назначьте администратором и отправьте /setup в нужной теме."
        )
        await self.say(update, text, Keyboard(buttons) if buttons else None)

    def current_group(self, context):
        return self.store.group(context.user_data.get("chat_id", 0))

    async def menu(self, update, context, notice=None):
        group = self.current_group(context)
        if not group:
            return await self.groups_menu(update)
        audience = len(self.store.audience(group))
        mode = "выбранные" if group["audience"] == "selected" else "все известные"
        status = "включены" if group["enabled"] else "на паузе"
        day = self.local_day(group)
        skip = self.store.skipped(group["chat_id"], day)
        ready = bool(audience and self.store.lessons(group["chat_id"]))
        checklist = (
            f"{'✅' if audience else '◻️'} Получатели   "
            f"{'✅' if self.store.lessons(group['chat_id']) else '◻️'} Пары   "
            f"{'✅' if group['panel_id'] else '◻️'} Кнопка в группе"
        )
        text = (
            f"☕ <b>ПараБот · {escape(group['title'])}</b>\n"
            f"{'Всё готово к занятиям' if ready else 'Давайте настроим группу'}\n\n"
            f"{checklist}\n\n"
            f"👥 Получатели: {audience} · {mode}\n"
            f"📚 Пар: {len(self.store.lessons(group['chat_id']))}\n"
            f"🌓 Сейчас: {WEEK_NAMES[week_parity(day, group)].lower()}\n"
            f"🔔 Автонапоминания: {status} · за {group['before_minutes']} мин\n"
            f"🌙 Расписание на завтра: {'в ' + group['tomorrow_time'] if group['tomorrow_enabled'] else 'выключено'}\n"
            + ("⛔ Сегодня все пары отменены\n" if self.store.day_cancelled(group["chat_id"], day) else "")
            + ("🔕 Сегодня напоминания отключены\n" if skip else "")
            + f"🕒 {escape(group['timezone'])}\n"
            f"💬 Уведомления: {'выбранная тема' if group['thread_id'] else 'общий чат / общая тема'}\n\n"
            "Начните с получателей и пар. Затем опубликуйте кнопку в группе.\n"
            "Пауза касается напоминаний перед парой. Рассылка на завтра управляется отдельно; «Отмечаемся» остаётся доступной."
        )
        if notice:
            text += "\n\n" + escape(notice)
        await self.say(
            update,
            text,
            Keyboard(
                [
                    [
                        Button("👥 Выбрать людей", callback_data="admin:members:0", style="primary"),
                        Button("➕ @теги", callback_data="admin:tags"),
                    ],
                    [
                        Button("📚 Расписание", callback_data="admin:schedule"),
                        Button("➕ Ввести пары", callback_data="admin:input", style="primary"),
                    ],
                    [Button("📅 Отмены и переносы", callback_data="admin:days", style="primary")],
                    [Button("✏️ Изменить постоянное расписание", callback_data="admin:editlist:0")],
                    [Button("🌓 Чётная / нечётная неделя", callback_data="admin:weeks", style="primary")],
                    [Button("🌙 Расписание на завтра", callback_data="admin:tomorrow", style="primary")],
                    [
                        Button("📥 CSV", callback_data="admin:csv"),
                        Button("🗑 Удалить пару", callback_data="admin:delete:0"),
                    ],
                    [
                        Button("⏸ Пауза" if group["enabled"] else "▶️ Включить", callback_data="admin:pause"),
                        Button("👥 Режим получателей", callback_data="admin:mode"),
                    ],
                    [
                        Button("🔔 За сколько минут", callback_data="admin:before"),
                        Button("🕒 Часовой пояс", callback_data="admin:timezone"),
                    ],
                    [
                        Button(
                            "↩️ Вернуть сегодня" if skip else "☕ Сегодня без напоминаний",
                            callback_data="admin:skip",
                        )
                    ],
                    [
                        Button("📌 Опубликовать кнопку", callback_data="admin:panel", style="primary"),
                        Button("📤 Выгрузить CSV", callback_data="admin:export"),
                    ],
                    [
                        Button("🧹 Очистить расписание", callback_data="admin:clear"),
                        Button("← Группы", callback_data="admin:groups"),
                    ],
                ]
            ),
        )

    async def tomorrow_menu(self, update, context, notice=None):
        group = self.current_group(context)
        enabled = bool(group["tomorrow_enabled"])
        text = (
            "🌙 <b>Ежедневное расписание на завтра</b>\n\n"
            f"Статус: {'включено' if enabled else 'выключено'}\n"
            f"Время отправки: <b>{group['tomorrow_time']}</b> · {escape(group['timezone'])}\n\n"
            "Каждый день бот отправляет в настроенную тему расписание следующего дня: "
            "с учётом чётности недели, отмен и переносов на момент отправки. "
            "Если пар нет или весь день отменён, бот сообщит об этом.\n\n"
            "Рассылка включается отдельно от напоминаний перед парой."
        )
        if notice:
            text += "\n\n" + escape(notice)
        await self.say(
            update,
            text,
            Keyboard(
                [
                    [
                        Button(
                            "🔕 Выключить рассылку" if enabled else "🔔 Включить рассылку",
                            callback_data=f"admin:tomorrowset:{0 if enabled else 1}",
                            style=None if enabled else "primary",
                        )
                    ],
                    [Button("🕒 Выбрать время", callback_data="admin:tomorrowtime")],
                    [Button("👀 Предпросмотр на завтра", callback_data="admin:tomorrowpreview")],
                    [Button("← Меню", callback_data="admin:menu")],
                ]
            ),
        )

    async def members_menu(self, update, context, page):
        group = self.current_group(context)
        members = self.store.members(group["chat_id"])
        page = min(max(0, page), max(0, (len(members) - 1) // PAGE))
        buttons = []
        for member in members[page * PAGE : (page + 1) * PAGE]:
            icon = "✅" if member["selected"] and member["present"] and not member["muted"] else "▫️"
            suffix = " · вышел" if not member["present"] else " · отключил" if member["muted"] else ""
            name = "@" + member["username"] if member["username"] else member["name"]
            buttons.append(
                [
                    Button(
                        f"{icon} {name}{suffix}"[:60], callback_data=f"admin:toggle:{member['id']}:{page}"
                    ),
                    Button("✕", callback_data=f"admin:remove:{member['id']}:{page}"),
                ]
            )
        nav = []
        if page:
            nav.append(Button("←", callback_data=f"admin:members:{page - 1}"))
        if (page + 1) * PAGE < len(members):
            nav.append(Button("→", callback_data=f"admin:members:{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons += [
            [
                Button("✅ Выбрать всех", callback_data="admin:selectall"),
                Button("▫️ Снять выбор", callback_data="admin:selectnone"),
            ],
            [
                Button("➕ Ввести @теги", callback_data="admin:tags"),
                Button("← Меню", callback_data="admin:menu"),
            ],
        ]
        await self.say(
            update,
            f"👥 <b>Получатели · страница {page + 1}</b>\n\nНажмите имя, чтобы включить / выключить. ✕ удаляет запись.\nНовые люди появляются после /join, кнопки «Я участник», сообщений, которые видит бот, или входа в группу. Автоматическое обнаружение не выбирает человека за вас.\n\nВсего известных: "
            + str(len(members)),
            Keyboard(buttons),
        )

    async def delete_menu(self, update, context, page):
        lessons = self.store.lessons(self.current_group(context)["chat_id"])
        page = min(max(0, page), max(0, (len(lessons) - 1) // PAGE))
        rows = [
            [
                Button(
                    lesson_button_text(item),
                    callback_data=f"admin:delask:{item['id']}:{page}",
                )
            ]
            for item in lessons[page * PAGE : (page + 1) * PAGE]
        ]
        nav = []
        if page:
            nav.append(Button("←", callback_data=f"admin:delete:{page - 1}"))
        if (page + 1) * PAGE < len(lessons):
            nav.append(Button("→", callback_data=f"admin:delete:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([Button("← Меню", callback_data="admin:menu")])
        await self.say(
            update, "🗑 Выберите пару для удаления." if lessons else "Пока нет пар.", Keyboard(rows)
        )

    async def export(self, update, context):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["day", "start", "end", "name", "room", "week"])
        for item in self.store.lessons(self.current_group(context)["chat_id"]):
            writer.writerow(
                [
                    item["on_date"] or ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")[item["day"]],
                    item["start"],
                    item["end"],
                    item["name"],
                    item["room"],
                    item["week"],
                ]
            )
        file = io.BytesIO(stream.getvalue().encode("utf-8-sig"))
        file.name = "schedule.csv"
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=file,
            caption="Постоянное расписание в CSV UTF-8. Отмены и переносы на дату хранятся в базе и в этот файл не входят.",
        )

    async def admin_callback(self, update, context):
        if not await self.owner_only(update):
            return
        if update.effective_chat.type != ChatType.PRIVATE:
            return await update.callback_query.answer("Откройте /admin в личке.", show_alert=True)
        # Old admin keyboards are tied to their group via each message, not the most recent context.
        data = update.callback_query.data.split(":")
        await update.callback_query.answer()
        context.user_data.pop("pending", None)
        if data[0] == "group":
            context.user_data.pop("draft", None)
            context.user_data.pop("schedule_draft", None)
            context.user_data.pop("schedule_input", None)
            context.user_data["chat_id"] = int(data[1])
            return await self.menu(update, context)
        # Group-specific callback routing is restored by an explicit suffix on buttons.
        if data[-1].startswith("g="):
            context.user_data["chat_id"] = int(data.pop()[2:])
        action = data[1]
        if action not in ("save", "discard"):
            context.user_data.pop("draft", None)
        if action not in ("editsave", "editdiscard"):
            context.user_data.pop("schedule_draft", None)
            context.user_data.pop("schedule_input", None)
        if action == "groups":
            return await self.groups_menu(update)
        group = self.current_group(context)
        if not group:
            return await self.groups_menu(update)
        chat_id = group["chat_id"]
        notice = None
        if action == "tomorrow":
            return await self.tomorrow_menu(update, context)
        if action == "tomorrowset":
            if data[2] in ("0", "1"):
                self.store.set_group(chat_id, tomorrow_enabled=int(data[2]))
            return await self.tomorrow_menu(update, context)
        if action == "tomorrowpreview":
            day = self.local_day(group) + timedelta(days=1)
            return await self.say(
                update,
                "🌙 <b>Предпросмотр расписания на завтра</b>\n" + day_text(self.store, group, day),
                Keyboard([[Button("← Рассылка", callback_data="admin:tomorrow")]]),
            )
        if action in WEEK_ACTIONS:
            return await self.week_action(update, context, data)
        if action in SCHEDULE_ACTIONS:
            return await self.schedule_action(update, context, data)
        if action == "menu":
            return await self.menu(update, context)
        if action == "members":
            return await self.members_menu(update, context, int(data[2]))
        if action in ("toggle", "remove"):
            member = self.store.member(chat_id, int(data[2]))
            if member:
                if action == "toggle":
                    self.store.select(chat_id, member["id"], not member["selected"] or bool(member["muted"]))
                else:
                    self.store.remove_member(chat_id, member["id"])
            return await self.members_menu(update, context, int(data[3]))
        if action in ("selectall", "selectnone"):
            self.store.select_all(chat_id, action == "selectall")
            return await self.members_menu(update, context, 0)
        if action == "mode":
            return await self.say(
                update,
                "Кого отмечать в уведомлениях?\n\n«Все известные» включает обнаруженных ботом участников группы. Старых молчащих участников нужно познакомить с ботом или добавить вручную.",
                Keyboard(
                    [
                        [Button("✅ Только выбранные", callback_data="admin:setmode:selected")],
                        [Button("👥 Все известные", callback_data="admin:setmode:all")],
                        [Button("← Меню", callback_data="admin:menu")],
                    ]
                ),
            )
        if action == "setmode":
            if data[2] in ("selected", "all"):
                self.store.set_group(chat_id, audience=data[2])
        elif action == "pause":
            self.store.set_group(chat_id, enabled=not group["enabled"])
        elif action == "skip":
            self.store.toggle_skip(chat_id, self.local_day(group))
        elif action == "panel":
            await self.publish_panel(chat_id)
            notice = "✅ Панель отправлена в тему. Её можно закрепить средствами Telegram."
        elif action == "schedule":
            return await self.say(
                update,
                self.schedule_text(chat_id),
                Keyboard([[Button("← Меню", callback_data="admin:menu")]]),
            )
        elif action == "export":
            return await self.export(update, context)
        elif action == "delete":
            return await self.delete_menu(update, context, int(data[2]))
        elif action == "delask":
            lesson = next((item for item in self.store.lessons(chat_id) if item["id"] == int(data[2])), None)
            if lesson:
                return await self.say(
                    update,
                    "Удалить эту пару?\n\n" + lesson_label(lesson),
                    Keyboard(
                        [
                            [
                                Button(
                                    "🗑 Удалить",
                                    callback_data=f"admin:delok:{data[2]}:{data[3]}",
                                    style="danger",
                                )
                            ],
                            [Button("Отмена", callback_data=f"admin:delete:{data[3]}")],
                        ]
                    ),
                )
        elif action == "delok":
            self.store.delete_lesson(chat_id, int(data[2]))
            return await self.delete_menu(update, context, int(data[3]))
        elif action == "clear":
            return await self.say(
                update,
                "Удалить всё расписание этой группы? Можно сначала выгрузить CSV.",
                Keyboard(
                    [
                        [Button("Да, удалить пары", callback_data="admin:clearok", style="danger")],
                        [Button("Отмена", callback_data="admin:menu")],
                    ]
                ),
            )
        elif action == "clearok":
            self.store.clear_lessons(chat_id)
        elif action in ("tags", "input", "csv", "timezone", "before", "tomorrowtime"):
            context.user_data["pending"] = (action, chat_id)
            prompts = {
                "tags": "Отправьте @username через пробел или с новой строки, до 100 за раз. Они сразу станут выбранными получателями.\n\nДля человека без username: /add ответом на его сообщение в группе.\n/cancel — отмена.",
                "input": "Отправьте одну или несколько строк:\n\n<code>Пн | 09:00-10:30 | Математика | 304\nВт | 12:40-14:10 | Английский\n2026-10-05 | 15:00-16:30 | Консультация | онлайн\nЧт | 19:30-21:00 | Семинар | 201 | нечетная</code>\n\nПоследние два поля необязательны. Настройка чётности и ввод двух расписаний — в «Чётная / нечётная неделя». Пары добавляются к имеющимся. Отмены и переносы на дату и изменение постоянного расписания доступны отдельными кнопками в меню.\n/cancel — отмена.",
                "csv": "Пришлите .csv файл до 100 КБ в UTF-8 с колонками <code>day,start,end,name,room,week</code>. Первые четыре обязательны. Импорт добавляет пары; точные дубликаты пропускаются. Перед сохранением покажу подтверждение.\n/cancel — отмена.",
                "timezone": "Отправьте часовой пояс IANA, например <code>Europe/Moscow</code>. Время в расписании считается местным временем группы.\n/cancel — отмена.",
                "before": "За сколько минут до пары напоминать? Введите целое число от 1 до 60. По умолчанию 5.\n/cancel — отмена.",
                "tomorrowtime": "В какое время ежедневно отправлять расписание на следующий день?\n\nВведите <code>23:30</code> или другое время от 00:00 до 23:59. Время считается по часовому поясу группы. После сохранения рассылка включится.\n/cancel — отмена.",
            }
            return await self.say(
                update,
                prompts[action],
                Keyboard([[Button("Отмена · вернуться", callback_data="admin:menu")]]),
            )
        elif action in ("save", "discard"):
            draft = context.user_data.get("draft")
            if not draft or len(data) < 3 or draft[2] != data[2] or draft[0] != chat_id:
                return await self.say(
                    update,
                    "Это подтверждение уже устарело. Введите пары заново через меню.",
                    Keyboard([[Button("← Меню", callback_data="admin:menu")]]),
                )
            context.user_data.pop("draft", None)
            if action == "save":
                count = self.store.add_lessons(chat_id, draft[1])
                notice = f"✅ Добавлено пар: {count}. Точные дубликаты пропущены."
        await self.menu(update, context, notice)

    async def input_message(self, update, context):
        if update.effective_chat.type != ChatType.PRIVATE or not self.is_admin(update.effective_user):
            return
        pending = context.user_data.get("pending")
        if not pending:
            return await self.say(update, "Откройте /admin, чтобы выбрать действие. /help — список команд.")
        action, chat_id = pending
        if not self.store.group(chat_id):
            context.user_data.pop("pending", None)
            return await self.groups_menu(update)
        text = update.effective_message.text or ""
        notice = "✅ Настройка сохранена."
        try:
            if action in ("daydate", "move", "edit"):
                return await self.schedule_input(update, context, action, chat_id, text)
            if action == "tags":
                tags = re.split(r"[\s,;]+", text.strip())
                if (
                    not text
                    or len(tags) > 100
                    or any(not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", t) for t in tags)
                ):
                    raise ValueError(
                        "Нужны @username через пробел: например @student_one @student_two. До 100 тегов; username 5–32 символа."
                    )
                for tag in set(tags):
                    self.store.add_username(chat_id, tag)
                notice = f"✅ Добавлено / выбрано тегов: {len(set(tags))}. Проверьте написание: чужой @username без ID проверить нельзя."
            elif action in ("input", "csv") or action.startswith(("weekinput_", "weekcsv_")):
                if action == "csv" or action.startswith("weekcsv_"):
                    document = update.effective_message.document
                    if not document or not (document.file_name or "").lower().endswith(".csv"):
                        raise ValueError("Отправьте именно файл .csv.")
                    if (document.file_size or 0) > 100_000:
                        raise ValueError("Файл должен быть не больше 100 КБ.")
                    file = await context.bot.get_file(document.file_id)
                    data = bytes(await file.download_as_bytearray())
                    lessons = parse_csv(data)
                else:
                    lessons = parse_lines(text)
                if action.startswith(("weekinput_", "weekcsv_")):
                    lessons = assign_week(lessons, action.split("_", 1)[1])
                if len(self.store.lessons(chat_id)) + len(lessons) > 500:
                    raise ValueError("В группе может быть до 500 пар; сначала удалите старые.")
                nonce = secrets.token_hex(4)
                context.user_data["draft"] = (chat_id, lessons, nonce)
                preview = "\n\n".join(lesson_label({**item.__dict__}) for item in lessons[:8])
                if len(lessons) > 8:
                    preview += f"\n\n… и ещё {len(lessons) - 8} пар."
                await self.say(
                    update,
                    f"Добавить {len(lessons)} пар в <b>{escape(self.store.group(chat_id)['title'])}</b>?\n\n"
                    + preview,
                    Keyboard(
                        [
                            [
                                Button(
                                    "✅ Сохранить",
                                    callback_data=f"admin:save:{nonce}:g={chat_id}",
                                    style="success",
                                ),
                                Button("Отмена", callback_data=f"admin:discard:{nonce}:g={chat_id}"),
                            ]
                        ]
                    ),
                )
                context.user_data.pop("pending", None)
                return
            elif action == "timezone":
                tz = text.strip()
                try:
                    ZoneInfo(tz)
                except (ZoneInfoNotFoundError, ValueError):
                    raise ValueError("Неизвестный часовой пояс. Пример: Europe/Moscow.") from None
                self.store.set_group(chat_id, timezone=tz)
            elif action == "before":
                if not text.strip().isdigit() or not 1 <= int(text.strip()) <= 60:
                    raise ValueError("Введите число от 1 до 60.")
                self.store.set_group(chat_id, before_minutes=int(text.strip()))
            elif action == "tomorrowtime":
                send_time = parse_time(text)
                self.store.set_group(chat_id, tomorrow_time=send_time, tomorrow_enabled=1)
                context.user_data.pop("pending", None)
                context.user_data["chat_id"] = chat_id
                return await self.tomorrow_menu(
                    update, context, f"✅ Каждый день в {send_time} будет приходить расписание на завтра."
                )
        except ValueError as exc:
            return await self.say(
                update, "Не сохранил: " + escape(str(exc)) + "\n\nИсправьте и отправьте снова или /cancel."
            )
        context.user_data.pop("pending", None)
        context.user_data["chat_id"] = chat_id
        await self.menu(update, context, notice)

    async def cancel(self, update, context):
        context.user_data.pop("pending", None)
        context.user_data.pop("draft", None)
        context.user_data.pop("schedule_draft", None)
        context.user_data.pop("schedule_input", None)
        if self.is_admin(update.effective_user) and update.effective_chat.type == ChatType.PRIVATE:
            await self.menu(update, context, "Ввод отменён.")
        else:
            await self.say(update, "Ввод отменён.")

    @staticmethod
    def member_present(member):
        return member.status in ("creator", "administrator", "member") or (
            member.status == "restricted" and member.is_member
        )

    async def public_callback(self, update, context):
        query = update.callback_query
        group = self.store.group(update.effective_chat.id)
        if not group or update.effective_chat.type not in GROUP_TYPES:
            return await query.answer("Группа ещё не настроена.", show_alert=True)
        try:
            member = await context.bot.get_chat_member(group["chat_id"], query.from_user.id)
        except TelegramError:
            return await query.answer("Не удалось проверить участие. Проверьте права бота.", show_alert=True)
        if not self.member_present(member) or query.from_user.is_bot:
            return await query.answer("Кнопка доступна участникам группы.", show_alert=True)
        action = query.data.split(":")[1]
        user = query.from_user
        self.store.observe(group["chat_id"], user.id, user.username, user.full_name)
        if action == "ping":
            now = self.service.clock().timestamp()
            remaining = group["cooldown_until"] - now
            if remaining > 0:
                seconds = math.ceil(remaining)
                return await query.answer(
                    f"Уже позвали! Ещё {seconds // 60}:{seconds % 60:02d} до следующего вызова.",
                    show_alert=True,
                )
            await query.answer("Зову участников…")
            result = await self.service.ping(group["chat_id"], user.full_name)
            if not result.startswith("Готово"):
                await self.say(update, result)
        elif action == "join":
            self.store.mute(group["chat_id"], user.id, False)
            await query.answer(
                "Готово! Администратор может выбрать вас. Повторное нажатие не вызывает уведомления.",
                show_alert=True,
            )
        elif action == "today":
            await query.answer()
            await self.say(update, today_text(self.store, group, self.service.clock()))

    async def observe(self, update, context):
        chat = update.effective_chat
        if not chat or chat.type not in GROUP_TYPES or not self.store.group(chat.id):
            return
        msg = update.effective_message
        if msg and msg.migrate_to_chat_id:
            log.warning(
                "Группа %s преобразована в супергруппу. Выполните /setup в новой группе и перенесите CSV/получателей.",
                chat.id,
            )
            self.store.set_group(chat.id, enabled=False, tomorrow_enabled=0)
            return
        if msg:
            users = list(msg.new_chat_members or [])
            if msg.from_user and not msg.sender_chat:
                users.append(msg.from_user)
            for u in users:
                if not u.is_bot:
                    self.store.observe(chat.id, u.id, u.username, u.full_name)
            if msg.left_chat_member and not msg.left_chat_member.is_bot:
                u = msg.left_chat_member
                self.store.observe(chat.id, u.id, u.username, u.full_name, False)

    async def member_changed(self, update, context):
        event = update.chat_member
        if event and self.store.group(event.chat.id):
            user = event.new_chat_member.user
            if not user.is_bot:
                self.store.observe(
                    event.chat.id,
                    user.id,
                    user.username,
                    user.full_name,
                    self.member_present(event.new_chat_member),
                )

    async def bot_changed(self, update, context):
        event = update.my_chat_member
        if event and self.store.group(event.chat.id) and event.new_chat_member.status in ("left", "kicked"):
            self.store.set_group(event.chat.id, enabled=False, tomorrow_enabled=0)

    async def on_error(self, update, context):
        log.error("Ошибка обработчика: %s", type(context.error).__name__)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await self.say(
                    update,
                    "Не получилось выполнить действие. Проверьте права бота и доступность темы, затем повторите. Подробности — в журнале запуска.",
                )
            except TelegramError:
                pass


def build_app(settings: Settings, request=None):
    store = Store(settings.db_path)
    builder = (
        Application.builder()
        .token(settings.token)
        .concurrent_updates(False)
        .connect_timeout(15)
        .read_timeout(20)
        .write_timeout(20)
        .pool_timeout(15)
        # getUpdates is a long poll, so its HTTP read timeout must exceed the
        # Bot API polling timeout passed to run_polling.
        .get_updates_connect_timeout(15)
        .get_updates_read_timeout(45)
        .get_updates_write_timeout(20)
        .get_updates_pool_timeout(15)
    )
    if request is not None:
        builder.request(request).get_updates_request(request)
    app = builder.build()
    service = Notifications(store, app.bot)
    ui = UI(settings, store, service, app)
    app.bot_data.update(store=store, service=service, ui=ui)

    commands = {
        "start": ui.start,
        "help": ui.help,
        "id": ui.show_id,
        "setup": ui.setup,
        "topic": ui.set_topic,
        "panel": ui.panel,
        "admin": ui.admin,
        "join": ui.join,
        "leave": ui.leave,
        "add": ui.add,
        "ping": ui.ping,
        "today": ui.today,
        "schedule": ui.schedule,
        "cancel": ui.cancel,
    }
    for name, handler in commands.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(ui.public_callback, pattern=r"^public:"))
    app.add_handler(CallbackQueryHandler(ui.admin_callback, pattern=r"^(admin:|group:)"))
    app.add_handler(
        MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.Document.ALL, ui.input_message)
    )
    app.add_handler(MessageHandler(filters.ALL, ui.observe), group=-1)
    app.add_handler(ChatMemberHandler(ui.member_changed, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(ui.bot_changed, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(ui.on_error)

    async def post_init(application):
        await application.bot.set_my_commands(
            [
                BotCommand(n, d)
                for n, d in (
                    ("ping", "Позвать выбранных участников"),
                    ("today", "Пары на сегодня"),
                    ("schedule", "Расписание"),
                    ("join", "Познакомить бота с собой"),
                    ("leave", "Отключить свои напоминания"),
                    ("admin", "Меню администратора"),
                    ("id", "Мой Telegram ID"),
                    ("help", "Помощь"),
                )
            ]
        )
        if not settings.admin_ids:
            log.warning(
                "ADMIN_IDS пуст. Напишите боту /id, внесите ID в .env и перезапустите. Управление пока закрыто."
            )
        await service.tick()
        application.job_queue.run_repeating(
            service.tick,
            interval=15,
            first=15,
            job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30},
        )
        log.info("ПараБот запущен. Настройки: /admin. Остановка: Ctrl+C.")

    async def post_shutdown(application):
        store.close()

    app.post_init, app.post_shutdown = post_init, post_shutdown
    return app
