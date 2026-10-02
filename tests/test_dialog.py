"""Process real Telegram Update objects through PTB, with all HTTP calls simulated."""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

from telegram import Update
from telegram.request import BaseRequest

from parabot.bot import build_app
from parabot.config import Settings
from parabot.schedule import parse_lines


class TelegramAPI(BaseRequest):
    def __init__(self):
        self.calls = []
        self.message_id = 500
        self.departed = set()
        self.files = {}

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        name = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((name, params))
        if "/file/" in url and name in self.files:
            return 200, self.files[name]
        bot = {"id": 999, "is_bot": True, "first_name": "ПараБот", "username": "parabot_test_bot"}
        result = True
        if name == "getMe":
            result = bot
        elif name == "getFile":
            file_id = params["file_id"]
            result = {
                "file_id": file_id,
                "file_unique_id": file_id,
                "file_size": len(self.files[file_id]),
                "file_path": file_id,
            }
        elif name == "getChatMember":
            uid = params["user_id"]
            user = bot if uid == 999 else {"id": uid, "is_bot": False, "first_name": "Студент"}
            if uid in self.departed:
                result = {"status": "left", "user": user}
            elif uid == 1:
                result = {"status": "creator", "user": user, "is_anonymous": False}
            elif uid == 999:
                result = {
                    "status": "administrator",
                    "user": user,
                    "is_anonymous": False,
                    "can_be_edited": True,
                    "can_manage_chat": True,
                    "can_delete_messages": False,
                    "can_manage_video_chats": False,
                    "can_restrict_members": False,
                    "can_promote_members": False,
                    "can_change_info": False,
                    "can_invite_users": False,
                    "can_post_stories": False,
                    "can_edit_stories": False,
                    "can_delete_stories": False,
                }
            else:
                result = {"status": "member", "user": user}
        elif name in ("sendMessage", "editMessageText", "sendDocument"):
            self.message_id += 1
            result = {
                "message_id": params.get("message_id", self.message_id),
                "date": 1790827200,
                "from": bot,
                "text": params.get("text", ""),
                "chat": {
                    "id": params["chat_id"],
                    "type": "private" if params["chat_id"] > 0 else "supergroup",
                },
            }
            if "reply_markup" in params:
                result["reply_markup"] = params["reply_markup"]
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def last(self, method):
        return next(params for name, params in reversed(self.calls) if name == method)


def message(uid, text, chat_id=1, thread=None, username=None):
    msg = {
        "message_id": 10,
        "date": 1790827200,
        "from": {"id": uid, "is_bot": False, "first_name": f"Студент {uid}"},
        "chat": {
            "id": chat_id,
            "type": "private" if chat_id > 0 else "supergroup",
            "title": "Учебная группа",
        },
        "text": text,
    }
    if username:
        msg["from"]["username"] = username
    if text.startswith("/"):
        msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if thread:
        msg["message_thread_id"] = thread
        msg["is_topic_message"] = True
    return {"update_id": 1, "message": msg}


def callback(uid, data, chat_id=1, message_id=501):
    return {
        "update_id": 2,
        "callback_query": {
            "id": "callback-id",
            "chat_instance": "instance",
            "from": {"id": uid, "is_bot": False, "first_name": f"Студент {uid}"},
            "data": data,
            "message": {
                "message_id": message_id,
                "date": 1790827200,
                "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
                "from": {"id": 999, "is_bot": True, "first_name": "ПараБот"},
                "text": "Панель",
            },
        },
    }


def test_complete_owner_setup_and_student_button_flow(tmp_path):
    async def scenario():
        api = TelegramAPI()
        app = build_app(Settings("999:FAKE_TOKEN", frozenset({1}), tmp_path / "bot.db"), request=api)
        await app.initialize()
        db = app.bot_data["store"]
        app.bot_data["service"].clock = lambda: datetime(2026, 10, 1, 5, 55, tzinfo=timezone.utc)

        async def process(data):
            await app.process_update(Update.de_json(data, app.bot))

        await process(message(1, "/setup", -100, 42))
        assert db.group(-100)["thread_id"] == 42
        await process(message(1, "/start manage_100"))
        keyboard = api.last("sendMessage")["reply_markup"]["inline_keyboard"]
        assert any(button.get("callback_data", "").endswith(":g=-100") for row in keyboard for button in row)
        sent_before = len([call for call in api.calls if call[0] == "sendMessage"])
        await process(callback(1, "admin:tags:g=-100"))
        assert "@username" in api.last("editMessageText")["text"]
        assert len([call for call in api.calls if call[0] == "sendMessage"]) == sent_before
        await process(message(1, "@student_one @student_two"))
        assert len(db.audience(db.group(-100))) == 2
        await process(callback(1, "admin:input:g=-100"))
        await process(message(1, "Чт | 09:00-10:30 | Матан | 304"))
        assert not db.lessons(-100)  # preview alone does not save
        keyboard = api.last("sendMessage")["reply_markup"]["inline_keyboard"]
        save = keyboard[0][0]["callback_data"]
        await process(callback(1, save))
        assert len(db.lessons(-100)) == 1
        await process(callback(1, "admin:panel:g=-100"))
        panel = api.last("sendMessage")
        assert panel["chat_id"] == -100 and panel["message_thread_id"] == 42
        assert db.group(-100)["panel_id"]
        await process(message(2, "/join", -100, 42, username="student_one"))
        assert any(m["user_id"] == 2 and m["selected"] for m in db.members(-100))
        await process(callback(2, "public:ping", -100, db.group(-100)["panel_id"]))
        ping = api.last("sendMessage")
        assert "Отмечаемся" in ping["text"] and "tg://user?id=2" in ping["text"]
        assert ping["message_thread_id"] == 42
        await process(callback(3, "public:ping", -100, db.group(-100)["panel_id"]))
        assert "5:00" in api.last("answerCallbackQuery")["text"]
        await process(message(3, "/ping", -100, 42))
        assert "Уже позвали" in api.last("sendMessage")["text"]
        await app.shutdown()
        db.close()

    asyncio.run(scenario())


def test_non_owner_cannot_change_group_or_private_settings(tmp_path):
    async def scenario():
        api = TelegramAPI()
        app = build_app(Settings("999:FAKE_TOKEN", frozenset({1}), tmp_path / "bot.db"), request=api)
        await app.initialize()
        db = app.bot_data["store"]
        await app.process_update(Update.de_json(message(2, "/setup", -100, 42), app.bot))
        assert db.group(-100) is None
        db.setup_group(-100, "Группа", 42, "Europe/Moscow")
        await app.process_update(Update.de_json(callback(2, "admin:pause:g=-100", chat_id=2), app.bot))
        assert db.group(-100)["enabled"] == 1
        assert "владельцу" in api.last("answerCallbackQuery")["text"]
        api.departed.add(3)
        await app.process_update(Update.de_json(callback(3, "public:ping", -100), app.bot))
        assert db.group(-100)["cooldown_until"] == 0
        assert "участникам" in api.last("answerCallbackQuery")["text"]
        await app.shutdown()
        db.close()

    asyncio.run(scenario())


def test_old_admin_menu_cannot_change_other_group_and_stale_draft_is_safe(tmp_path):
    async def scenario():
        api = TelegramAPI()
        app = build_app(Settings("999:FAKE_TOKEN", frozenset({1}), tmp_path / "bot.db"), request=api)
        await app.initialize()
        db = app.bot_data["store"]
        db.setup_group(-100, "Группа А", 42, "Europe/Moscow")
        db.setup_group(-200, "Группа Б", 84, "Europe/Moscow")

        async def process(data):
            await app.process_update(Update.de_json(data, app.bot))

        await process(callback(1, "group:-200"))
        await process(callback(1, "admin:pause:g=-100"))
        assert db.group(-100)["enabled"] == 0
        assert db.group(-200)["enabled"] == 1
        await process(callback(1, "admin:input:g=-100"))
        await process(message(1, "Пн | 9:00-10:00 | Старая"))
        stale_save = api.last("sendMessage")["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        await process(callback(1, "admin:input:g=-100"))
        await process(message(1, "Пн | 10:00-11:00 | Новая"))
        fresh_save = api.last("sendMessage")["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        await process(callback(1, stale_save))
        assert not db.lessons(-100)
        await process(callback(1, fresh_save))
        assert len(db.lessons(-100)) == 1 and db.lessons(-100)[0]["name"] == "Новая"
        await app.shutdown()
        db.close()

    asyncio.run(scenario())


async def dated_app(tmp_path):
    api = TelegramAPI()
    app = build_app(Settings("999:FAKE_TOKEN", frozenset({1}), tmp_path / "bot.db"), request=api)
    await app.initialize()
    db = app.bot_data["store"]
    db.setup_group(-100, "Учебная группа", 42, "Europe/Moscow")
    db.add_lessons(-100, parse_lines("Чт | 09:00-10:30 | Матан | 304\nЧт | 11:00-12:30 | Физика | 201"))
    member_id = db.observe(-100, 2, "student_two", "Студент")
    db.select(-100, member_id, True)
    now = [datetime(2026, 10, 1, 5, 54, 59, tzinfo=timezone.utc)]
    app.bot_data["service"].clock = lambda: now[0]
    return app, api, db, now


def button_data(api, label, method="editMessageText"):
    return next(
        button["callback_data"]
        for row in api.last(method)["reply_markup"]["inline_keyboard"]
        for button in row
        if label in button["text"]
    )


def test_cancel_class_five_minutes_before_start_and_restore_via_buttons(tmp_path):
    async def scenario():
        app, api, db, now = await dated_app(tmp_path)

        async def process(data):
            await app.process_update(Update.de_json(data, app.bot))

        await process(callback(1, "admin:days:g=-100"))
        await process(callback(1, button_data(api, "Сегодня")))
        await process(callback(1, button_data(api, "Матан")))
        await process(callback(1, button_data(api, "Отменить эту пару")))
        now[0] = datetime(2026, 10, 1, 5, 55, tzinfo=timezone.utc)
        count = len([call for call in api.calls if call[0] == "sendMessage"])
        await app.bot_data["service"].tick()
        assert len([call for call in api.calls if call[0] == "sendMessage"]) == count
        await process(message(2, "/today", -100, 42))
        assert "⛔ Отменена" in api.last("sendMessage")["text"]
        assert "Физика" in api.last("sendMessage")["text"]
        await process(callback(1, button_data(api, "Вернуть исходную пару")))
        await app.bot_data["service"].tick()
        assert "Через 5 мин" in api.last("sendMessage")["text"]
        assert api.last("sendMessage")["message_thread_id"] == 42
        assert len(db.occurrences(-100, date(2026, 10, 8))) == 2
        await app.shutdown()
        db.close()

    asyncio.run(scenario())


def test_move_preview_validation_save_and_today_view(tmp_path):
    async def scenario():
        app, api, db, now = await dated_app(tmp_path)
        original = db.lessons(-100)[0]

        async def process(data):
            await app.process_update(Update.de_json(data, app.bot))

        await process(callback(1, f"admin:move:{original['id']}:20261001:20261001:g=-100"))
        await process(message(1, "2026-10-02 | 12:30-11:00"))
        assert "Не сохранил" in api.last("sendMessage")["text"]
        await process(message(1, "2026-10-02 | 11:00-12:30 | 202"))
        assert "Было:" in api.last("sendMessage")["text"]
        assert "Будет:" in api.last("sendMessage")["text"]
        assert not db.changes_on(-100, date(2026, 10, 1))
        await process(callback(1, button_data(api, "Сохранить", "sendMessage")))
        assert db.lessons(-100)[0] == original
        moved = db.occurrences(-100, date(2026, 10, 2))[0]
        assert moved["room"] == "202"
        await process(message(2, "/today", -100, 42))
        assert "перенесена на 2026-10-02" in api.last("sendMessage")["text"]
        now[0] = datetime(2026, 10, 2, 7, 55, tzinfo=timezone.utc)
        await app.bot_data["service"].tick()
        assert "11:00–12:30" in api.last("sendMessage")["text"]
        assert "202" in api.last("sendMessage")["text"]
        await process(message(2, "/today", -100, 42))
        assert "Перенесена / изменена" in api.last("sendMessage")["text"]
        await app.shutdown()
        db.close()

    asyncio.run(scenario())


def test_future_day_cancellation_confirmation_and_non_owner_access(tmp_path):
    async def scenario():
        app, api, db, now = await dated_app(tmp_path)

        async def process(data):
            await app.process_update(Update.de_json(data, app.bot))

        await process(callback(1, "admin:daydate:g=-100"))
        await process(message(1, "08.10.2026"))
        await process(callback(1, button_data(api, "Отменить все пары", "sendMessage")))
        assert not db.day_cancelled(-100, date(2026, 10, 8))
        confirm = button_data(api, "Да, отменить")
        await process(callback(2, confirm, chat_id=2))
        assert "владельцу" in api.last("answerCallbackQuery")["text"]
        assert not db.day_cancelled(-100, date(2026, 10, 8))
        now[0] = datetime(2026, 10, 2, 5, 55, tzinfo=timezone.utc)
        await process(callback(1, confirm))
        assert db.day_cancelled(-100, date(2026, 10, 8))
        assert not db.day_cancelled(-100, date(2026, 10, 2))
        now[0] = datetime(2026, 10, 8, 5, 55, tzinfo=timezone.utc)
        await process(message(2, "/today", -100, 42))
        assert "Все пары на этот день отменены" in api.last("sendMessage")["text"]
        count = len([call for call in api.calls if call[0] == "sendMessage"])
        await app.bot_data["service"].tick()
        assert len([call for call in api.calls if call[0] == "sendMessage"]) == count
        await process(callback(1, button_data(api, "Восстановить день")))
        assert not db.day_cancelled(-100, date(2026, 10, 8))
        await app.shutdown()
        db.close()

    asyncio.run(scenario())


def test_permanent_edit_preview_and_stale_confirmation(tmp_path):
    async def scenario():
        app, api, db, now = await dated_app(tmp_path)
        original = db.lessons(-100)[0]

        async def process(data):
            await app.process_update(Update.de_json(data, app.bot))

        await process(callback(1, f"admin:editask:{original['id']}:g=-100"))
        await process(message(1, "Пт | 10:00-11:30 | Новый матан | 202 | четная"))
        stale = button_data(api, "Сохранить", "sendMessage")
        assert db.lesson(-100, original["id"]) == original
        await process(callback(1, "admin:menu:g=-100"))
        await process(callback(1, stale))
        assert db.lesson(-100, original["id"]) == original
        assert "устарело" in api.last("editMessageText")["text"]
        await process(callback(1, f"admin:editask:{original['id']}:g=-100"))
        await process(message(1, "Пт | 10:00-11:30 | Новый матан | 202 | четная"))
        await process(callback(1, button_data(api, "Сохранить", "sendMessage")))
        changed = db.lesson(-100, original["id"])
        assert (changed["day"], changed["start"], changed["week"], changed["room"]) == (
            4,
            "10:00",
            "even",
            "202",
        )
        assert len(db.lessons(-100)) == 2
        await app.shutdown()
        db.close()

    asyncio.run(scenario())


def test_move_confirmation_rechecks_changed_instance_and_elapsed_time(tmp_path):
    async def scenario():
        app, api, db, now = await dated_app(tmp_path)
        lesson_id = db.lessons(-100)[0]["id"]

        async def process(data):
            await app.process_update(Update.de_json(data, app.bot))

        move = f"admin:move:{lesson_id}:20261001:20261001:g=-100"
        await process(callback(1, move))
        await process(message(1, "11:00-12:30"))
        save = button_data(api, "Сохранить", "sendMessage")
        db.cancel_occurrence(-100, lesson_id, date(2026, 10, 1))
        await process(callback(1, save))
        assert "изменилась после предпросмотра" in api.last("editMessageText")["text"]
        assert db.occurrence(-100, lesson_id, date(2026, 10, 1))["cancelled"]
        db.restore_occurrence(-100, lesson_id, date(2026, 10, 1))
        await process(callback(1, move))
        await process(message(1, "09:01-10:31"))
        save = button_data(api, "Сохранить", "sendMessage")
        now[0] = datetime(2026, 10, 1, 6, 2, tzinfo=timezone.utc)
        await process(callback(1, save))
        assert "Новое время уже наступило" in api.last("editMessageText")["text"]
        assert not db.changes_on(-100, date(2026, 10, 1))
        await app.shutdown()
        db.close()

    asyncio.run(scenario())


def test_date_menu_pagination_local_timezone_and_callback_size(tmp_path):
    async def scenario():
        app, api, db, now = await dated_app(tmp_path)
        chat_id = -1001234567890
        db.setup_group(chat_id, "Большая группа", None, "Europe/Moscow")
        db.add_lessons(chat_id, parse_lines("\n".join(f"Пт | 09:00-10:30 | Предмет {i}" for i in range(10))))
        now[0] = datetime(2026, 10, 1, 21, 5, tzinfo=timezone.utc)

        async def process(data):
            await app.process_update(Update.de_json(data, app.bot))

        await process(callback(1, f"admin:days:g={chat_id}"))
        assert "20261002" in button_data(api, "Сегодня")
        await process(callback(1, button_data(api, "Сегодня")))
        keyboard = api.last("editMessageText")["reply_markup"]["inline_keyboard"]
        assert sum("Предмет" in b["text"] for row in keyboard for b in row) == 8
        await process(callback(1, "admin:day:20261002:1:g=-1001234567890"))
        keyboard = api.last("editMessageText")["reply_markup"]["inline_keyboard"]
        assert sum("Предмет" in b["text"] for row in keyboard for b in row) == 2
        await process(callback(1, button_data(api, "Предмет")))
        for name, params in api.calls:
            for row in params.get("reply_markup", {}).get("inline_keyboard", []):
                for button in row:
                    assert len(button.get("callback_data", "").encode()) <= 64
        await app.shutdown()
        db.close()

    asyncio.run(scenario())


def test_two_week_templates_added_in_menu_switch_automatically(tmp_path):
    async def scenario():
        app, api, db, now = await dated_app(tmp_path)

        async def process(data):
            await app.process_update(Update.de_json(data, app.bot))

        await process(callback(1, "admin:weeks:g=-100"))
        await process(callback(1, button_data(api, "Эта неделя нечётная")))
        assert db.group(-100)["week_anchor_parity"] == "odd"
        for week, label, name in (
            ("even", "Чётная неделя", "Физика чётная"),
            ("odd", "Нечётная неделя", "Матан нечётная"),
        ):
            await process(callback(1, "admin:weeks:g=-100"))
            await process(callback(1, button_data(api, label)))
            await process(callback(1, button_data(api, "Ввести расписание")))
            await process(message(1, f"Пн | 09:00-10:30 | {name} | 202"))
            assert not any(item["name"] == name for item in db.lessons(-100))
            await process(callback(1, button_data(api, "Сохранить", "sendMessage")))
            assert next(item for item in db.lessons(-100) if item["name"] == name)["week"] == week
        now[0] = datetime(2026, 10, 5, 5, 55, tzinfo=timezone.utc)
        await process(message(2, "/today", -100, 42))
        assert "Чётная неделя" in api.last("sendMessage")["text"]
        assert "Физика чётная" in api.last("sendMessage")["text"]
        assert "Матан нечётная" not in api.last("sendMessage")["text"]
        await app.bot_data["service"].tick()
        assert "📚 Физика чётная" in api.last("sendMessage")["text"]
        now[0] += timedelta(days=7)
        await process(message(2, "/today", -100, 42))
        assert "Нечётная неделя" in api.last("sendMessage")["text"]
        assert "Матан нечётная" in api.last("sendMessage")["text"]
        assert "Физика чётная" not in api.last("sendMessage")["text"]
        await app.bot_data["service"].tick()
        assert "📚 Матан нечётная" in api.last("sendMessage")["text"]
        await app.shutdown()
        db.close()

    asyncio.run(scenario())


def test_week_input_rejects_dates_and_conflicting_parity_and_invalidates_old_preview(tmp_path):
    async def scenario():
        app, api, db, now = await dated_app(tmp_path)

        async def process(data):
            await app.process_update(Update.de_json(data, app.bot))

        await process(callback(1, "admin:weekinput:odd:g=-100"))
        await process(message(1, "2026-10-05 | 09:00-10:30 | Разовая"))
        assert "нужны дни" in api.last("sendMessage")["text"]
        await process(message(1, "Пн | 09:00-10:30 | Ошибка || четная"))
        assert "не совпадает" in api.last("sendMessage")["text"]
        await process(message(1, "Пн | 09:00-10:30 | Старая"))
        stale = button_data(api, "Сохранить", "sendMessage")
        await process(callback(1, "admin:weekinput:even:g=-100"))
        await process(callback(1, stale))
        assert not any(item["name"] == "Старая" for item in db.lessons(-100))
        assert "устарело" in api.last("editMessageText")["text"]
        await app.shutdown()
        db.close()

    asyncio.run(scenario())


def test_csv_week_template_without_week_column_gets_selected_parity(tmp_path):
    async def scenario():
        app, api, db, now = await dated_app(tmp_path)
        api.files["even.csv"] = b"day,start,end,name,room\nMon,09:00,10:30,Even class,304"
        await app.process_update(Update.de_json(callback(1, "admin:weekcsv:even:g=-100"), app.bot))
        data = message(1, "")
        data["message"].pop("text")
        data["message"]["document"] = {
            "file_id": "even.csv",
            "file_unique_id": "even",
            "file_name": "even.csv",
            "file_size": len(api.files["even.csv"]),
        }
        await app.process_update(Update.de_json(data, app.bot))
        assert "чётная неделя" in api.last("sendMessage")["text"]
        await app.process_update(
            Update.de_json(callback(1, button_data(api, "Сохранить", "sendMessage")), app.bot)
        )
        assert next(item for item in db.lessons(-100) if item["name"] == "Even class")["week"] == "even"
        await app.shutdown()
        db.close()

    asyncio.run(scenario())


def test_week_cycle_settings_in_old_menu_remain_bound_to_group_and_reference_week(tmp_path):
    async def scenario():
        app, api, db, now = await dated_app(tmp_path)
        db.setup_group(-200, "Другая группа", None, "Europe/Moscow")

        async def process(data):
            await app.process_update(Update.de_json(data, app.bot))

        await process(callback(1, "admin:weeks:g=-100"))
        old_button = button_data(api, "Эта неделя чётная")
        await process(callback(1, "group:-200"))
        now[0] += timedelta(days=7)
        await process(callback(2, old_button, chat_id=2))
        assert db.group(-100)["week_anchor"] == ""
        await process(callback(1, old_button))
        assert db.group(-100)["week_anchor"] == "2026-09-28"
        assert db.group(-200)["week_anchor"] == ""
        assert "Сейчас: <b>Нечётная неделя" in api.last("editMessageText")["text"]
        await process(callback(1, button_data(api, "Считать по календарю")))
        assert db.group(-100)["week_anchor"] == ""
        await app.shutdown()
        db.close()

    asyncio.run(scenario())


def test_ping_command_buttons_and_restart_share_one_non_extending_cooldown(tmp_path):
    async def scenario():
        app, api, db, now = await dated_app(tmp_path)

        async def process(data):
            await app.process_update(Update.de_json(data, app.bot))

        def ping_count():
            return sum(
                name == "sendMessage" and "<b>Отмечаемся!</b>" in params.get("text", "")
                for name, params in api.calls
            )

        await process(message(2, "/ping", -100, 42))
        until = db.group(-100)["cooldown_until"]
        assert ping_count() == 1
        for i in range(20):
            now[0] += timedelta(seconds=10)
            await process(callback(i + 3, "public:ping", -100, message_id=100 + i))
            await process(message(i + 3, "/ping", -100, 42))
            assert db.group(-100)["cooldown_until"] == until
        assert ping_count() == 1
        await app.shutdown()
        db.close()
        app = build_app(Settings("999:FAKE_TOKEN", frozenset({1}), tmp_path / "bot.db"), request=api)
        await app.initialize()
        app.bot_data["service"].clock = lambda: now[0]
        db = app.bot_data["store"]
        now[0] += timedelta(seconds=99)
        await process(callback(42, "public:ping", -100, message_id=7))
        assert "0:01" in api.last("answerCallbackQuery")["text"]
        assert ping_count() == 1
        now[0] += timedelta(seconds=1)
        await process(callback(43, "public:ping", -100, message_id=8))
        assert ping_count() == 2
        await process(message(44, "/ping", -100, 42))
        assert ping_count() == 2
        await app.shutdown()
        db.close()

    asyncio.run(scenario())


def test_daily_summary_time_preview_send_and_disable_flow(tmp_path):
    async def scenario():
        app, api, db, now = await dated_app(tmp_path)
        db.add_lessons(-100, parse_lines("Пт | 09:00-10:30 | Завтрашняя физика | 304"))

        async def process(data):
            await app.process_update(Update.de_json(data, app.bot))

        await process(callback(1, "admin:tomorrow:g=-100"))
        assert "выключено" in api.last("editMessageText")["text"]
        assert "23:30" in api.last("editMessageText")["text"]
        await process(callback(1, button_data(api, "Выбрать время")))
        await process(message(1, "25:30"))
        assert "Не сохранил" in api.last("sendMessage")["text"]
        assert db.group(-100)["tomorrow_enabled"] == 0
        await process(message(1, "23:30"))
        assert db.group(-100)["tomorrow_enabled"] == 1
        assert db.group(-100)["tomorrow_time"] == "23:30"
        assert "Каждый день в 23:30" in api.last("sendMessage")["text"]
        before = len([call for call in api.calls if call[0] == "sendMessage" and call[1]["chat_id"] == -100])
        await process(callback(1, button_data(api, "Предпросмотр", "sendMessage")))
        assert "Завтрашняя физика" in api.last("editMessageText")["text"]
        assert "02.10.2026" in api.last("editMessageText")["text"]
        assert (
            len([call for call in api.calls if call[0] == "sendMessage" and call[1]["chat_id"] == -100])
            == before
        )
        assert not db.rows("SELECT * FROM summary_deliveries")
        now[0] = datetime(2026, 10, 1, 20, 30, tzinfo=timezone.utc)
        await app.bot_data["service"].tick()
        sent = api.last("sendMessage")
        assert sent["chat_id"] == -100 and sent["message_thread_id"] == 42
        assert "Расписание на завтра" in sent["text"] and "Завтрашняя физика" in sent["text"]
        await process(callback(1, "admin:tomorrow:g=-100"))
        await process(callback(1, button_data(api, "Выключить рассылку")))
        assert db.group(-100)["tomorrow_enabled"] == 0
        assert db.group(-100)["tomorrow_time"] == "23:30"
        count = len([call for call in api.calls if call[0] == "sendMessage" and call[1]["chat_id"] == -100])
        now[0] += timedelta(days=1)
        await app.bot_data["service"].tick()
        assert (
            len([call for call in api.calls if call[0] == "sendMessage" and call[1]["chat_id"] == -100])
            == count
        )
        await app.shutdown()
        db.close()

    asyncio.run(scenario())


def test_daily_settings_are_owner_only_and_old_buttons_target_their_original_group(tmp_path):
    async def scenario():
        app, api, db, now = await dated_app(tmp_path)
        db.setup_group(-200, "Вторая группа", None, "Europe/Moscow")

        async def process(data):
            await app.process_update(Update.de_json(data, app.bot))

        await process(callback(1, "admin:tomorrow:g=-100"))
        enable = button_data(api, "Включить рассылку")
        change_time = button_data(api, "Выбрать время")
        await process(callback(1, "group:-200"))
        await process(callback(2, enable, chat_id=2))
        assert "владельцу" in api.last("answerCallbackQuery")["text"]
        assert db.group(-100)["tomorrow_enabled"] == 0
        await process(callback(1, enable))
        assert db.group(-100)["tomorrow_enabled"] == 1
        assert db.group(-200)["tomorrow_enabled"] == 0
        await process(callback(1, change_time))
        await process(message(1, "9:05"))
        assert db.group(-100)["tomorrow_time"] == "09:05"
        assert db.group(-200)["tomorrow_time"] == "23:30"
        await process(callback(1, change_time))
        await process(message(1, "/cancel"))
        await process(message(1, "22:00"))
        assert db.group(-100)["tomorrow_time"] == "09:05"
        await app.shutdown()
        db.close()

    asyncio.run(scenario())
