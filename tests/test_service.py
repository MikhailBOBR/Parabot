import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree

import pytest
from telegram.error import BadRequest, NetworkError, RetryAfter

from parabot.db import Store
from parabot.messages import mention, notification_chunks, tomorrow_chunks
from parabot.schedule import parse_lines
from parabot.service import Notifications


def fixture():
    db = Store(":memory:")
    db.setup_group(-100, "Группа", 42, "Europe/Moscow")
    one = db.observe(-100, 1, "student_one", "Алиса")
    db.observe(-100, 2, "student_two", "Борис")
    db.select(-100, one, True)
    db.add_lessons(-100, parse_lines("Чт | 09:00-10:30 | Матан <&> | 304"))
    bot = SimpleNamespace(send_message=AsyncMock(), edit_message_reply_markup=AsyncMock())
    now = [datetime(2026, 10, 1, 5, 55, tzinfo=timezone.utc)]
    return db, bot, now, Notifications(db, bot, lambda: now[0])


def test_auto_reminder_correct_topic_mentions_and_no_duplicate():
    async def scenario():
        db, bot, now, service = fixture()
        await service.tick()
        await service.tick()
        assert bot.send_message.await_count == 1
        args = bot.send_message.call_args.kwargs
        assert args["chat_id"] == -100 and args["message_thread_id"] == 42
        assert "tg://user?id=1" in args["text"]
        assert "tg://user?id=2" not in args["text"]
        assert "Матан &lt;&amp;&gt;" in args["text"]
        assert args["disable_notification"] is False
        assert db.row("SELECT state FROM deliveries")["state"] == "sent"
        # A new service instance (like restarting) respects durable delivery state.
        await Notifications(db, bot, lambda: now[0]).tick()
        assert bot.send_message.await_count == 1
        db.close()

    asyncio.run(scenario())


def test_20_simultaneous_clicks_only_send_once_then_unlock():
    async def scenario():
        db, bot, now, service = fixture()
        results = await asyncio.gather(*(service.ping(-100, f"Студент {n}") for n in range(20)))
        assert sum(r.startswith("Готово") for r in results) == 1
        assert bot.send_message.await_count == 1
        now[0] += timedelta(seconds=299)
        assert "0:01" in await service.ping(-100, "Алиса")
        now[0] += timedelta(seconds=1)
        assert (await service.ping(-100, "Алиса")).startswith("Готово")
        assert bot.send_message.await_count == 2
        db.close()

    asyncio.run(scenario())


def test_empty_audience_does_not_consume_cooldown():
    async def scenario():
        db, bot, now, service = fixture()
        db.select_all(-100, False)
        assert "пуст" in await service.ping(-100, "Алиса")
        assert db.group(-100)["cooldown_until"] == 0
        bot.send_message.assert_not_awaited()
        db.close()

    asyncio.run(scenario())


def test_pause_and_day_off_only_stop_auto_reminders():
    async def scenario():
        db, bot, now, service = fixture()
        db.set_group(-100, enabled=0)
        await service.tick()
        bot.send_message.assert_not_awaited()
        assert (await service.ping(-100, "Алиса")).startswith("Готово")
        db.set_group(-100, enabled=1)
        db.toggle_skip(-100, now[0].date())
        await service.tick()
        assert bot.send_message.await_count == 1
        db.close()

    asyncio.run(scenario())


def test_unknown_delivery_keeps_cooldown_and_avoids_auto_duplicates():
    async def scenario():
        db, bot, now, service = fixture()
        bot.send_message.side_effect = NetworkError("timeout")
        assert "неизвестен" in await service.ping(-100, "Алиса")
        assert db.group(-100)["cooldown_until"] > now[0].timestamp()
        await service.tick()
        assert db.row("SELECT state FROM deliveries")["state"] == "uncertain"
        await service.tick()
        assert bot.send_message.await_count == 2
        db.close()

    asyncio.run(scenario())


def test_definite_rejection_releases_manual_cooldown_and_marks_auto_failure():
    async def scenario():
        db, bot, now, service = fixture()
        bot.send_message.side_effect = BadRequest("message thread not found")
        assert "отклонил" in await service.ping(-100, "Алиса")
        assert db.group(-100)["cooldown_until"] == 0
        await service.tick()
        assert db.row("SELECT state FROM deliveries")["state"] == "failed"
        db.close()

    asyncio.run(scenario())


def test_rate_limit_safe_retry_within_reminder_window():
    async def scenario():
        db, bot, now, service = fixture()
        bot.send_message.side_effect = [RetryAfter(20), SimpleNamespace(message_id=1)]
        await service.tick()
        assert db.row("SELECT * FROM deliveries") is None
        await service.tick()
        assert bot.send_message.await_count == 1
        now[0] += timedelta(seconds=21)
        await service.tick()
        assert db.row("SELECT state FROM deliveries")["state"] == "sent"
        assert bot.send_message.await_count == 2
        db.close()

    asyncio.run(scenario())


def test_chunks_do_not_cut_html_and_mentions_escape_names():
    people = [{"user_id": i + 1, "username": None, "name": '<&"' * 20} for i in range(150)]
    chunks = notification_chunks("<b>Пара!</b>", people, "До встречи!")
    assert len(chunks) > 1
    assert all(len(c) < 3600 for c in chunks)
    combined = "".join(chunks)
    assert combined.count('href="tg://user?id=') == 150
    assert combined.count("</a>") == 150
    assert "&lt;&amp;&quot;" in mention(people[0])


def test_reminder_waiting_for_lock_is_not_sent_after_class_starts():
    async def scenario():
        db, bot, now, service = fixture()
        await service.locks[-100].acquire()
        task = asyncio.create_task(service.tick())
        await asyncio.sleep(0)
        now[0] += timedelta(minutes=5)
        service.locks[-100].release()
        await task
        bot.send_message.assert_not_awaited()
        db.close()

    asyncio.run(scenario())


def test_rate_limit_stops_other_reminders_in_same_group():
    async def scenario():
        db, bot, now, service = fixture()
        db.add_lessons(-100, parse_lines("Чт | 09:00-10:30 | Вторая пара"))
        bot.send_message.side_effect = RetryAfter(30)
        await service.tick()
        assert bot.send_message.await_count == 1
        assert db.rows("SELECT * FROM deliveries") == []
        db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["cancel", "cancel-day", "move", "delete", "edit"])
def test_last_minute_change_is_rechecked_after_waiting_for_lock(change):
    async def scenario():
        db, bot, now, service = fixture()
        lesson_id = db.lessons(-100)[0]["id"]
        day = date(2026, 10, 1)
        await service.locks[-100].acquire()
        task = asyncio.create_task(service.tick())
        await asyncio.sleep(0)
        if change == "cancel":
            db.cancel_occurrence(-100, lesson_id, day)
        elif change == "cancel-day":
            db.set_day_cancelled(-100, day, True)
        elif change == "move":
            db.move_occurrence(-100, lesson_id, day, day, "11:00", "12:30")
        elif change == "delete":
            db.delete_lesson(-100, lesson_id)
        else:
            db.update_lesson(-100, lesson_id, parse_lines("Чт | 11:00-12:30 | Матан")[0])
        service.locks[-100].release()
        await task
        bot.send_message.assert_not_awaited()
        assert not db.rows("SELECT * FROM deliveries")
        db.close()

    asyncio.run(scenario())


def test_reschedule_after_reminder_sends_at_new_time_only_once():
    async def scenario():
        db, bot, now, service = fixture()
        lesson_id = db.lessons(-100)[0]["id"]
        day = date(2026, 10, 1)
        await service.tick()
        assert bot.send_message.await_count == 1
        db.move_occurrence(-100, lesson_id, day, day, "11:00", "12:30", "202")
        await service.tick()
        assert bot.send_message.await_count == 1
        now[0] = datetime(2026, 10, 1, 7, 55, tzinfo=timezone.utc)
        await service.tick()
        await service.tick()
        assert bot.send_message.await_count == 2
        assert "11:00–12:30" in bot.send_message.call_args.kwargs["text"]
        assert "202" in bot.send_message.call_args.kwargs["text"]
        now[0] = datetime(2026, 10, 8, 5, 55, tzinfo=timezone.utc)
        await service.tick()
        assert bot.send_message.await_count == 3
        assert "09:00–10:30" in bot.send_message.call_args.kwargs["text"]
        db.close()

    asyncio.run(scenario())


def test_moved_midnight_lesson_and_destination_day_cancellation():
    async def scenario():
        db, bot, now, service = fixture()
        lesson_id = db.lessons(-100)[0]["id"]
        source, target = date(2026, 10, 1), date(2026, 10, 2)
        db.move_occurrence(-100, lesson_id, source, target, "00:02", "01:00")
        now[0] = datetime(2026, 10, 1, 20, 57, tzinfo=timezone.utc)
        db.set_day_cancelled(-100, target, True)
        await service.tick()
        bot.send_message.assert_not_awaited()
        db.set_day_cancelled(-100, target, False)
        await service.tick()
        await service.tick()
        assert bot.send_message.await_count == 1
        assert db.rows("SELECT date FROM deliveries") == [{"date": source.isoformat()}]
        db.close()

    asyncio.run(scenario())


def test_two_instances_of_same_lesson_on_one_day_have_independent_reminders():
    async def scenario():
        db, bot, now, service = fixture()
        lesson_id = db.lessons(-100)[0]["id"]
        source, target = date(2026, 10, 1), date(2026, 10, 8)
        db.move_occurrence(-100, lesson_id, source, target, "11:00", "12:30")
        now[0] = datetime(2026, 10, 8, 5, 55, tzinfo=timezone.utc)
        await service.tick()
        now[0] = datetime(2026, 10, 8, 7, 55, tzinfo=timezone.utc)
        await service.tick()
        await service.tick()
        assert bot.send_message.await_count == 2
        assert {r["date"] for r in db.rows("SELECT date FROM deliveries")} == {
            source.isoformat(),
            target.isoformat(),
        }
        db.close()

    asyncio.run(scenario())


def test_move_inside_reminder_window_notifies_immediately_before_start():
    async def scenario():
        db, bot, now, service = fixture()
        day = date(2026, 10, 1)
        lesson_id = db.lessons(-100)[0]["id"]
        now[0] = datetime(2026, 10, 1, 5, 59, tzinfo=timezone.utc)
        db.move_occurrence(-100, lesson_id, day, day, "09:01", "10:31")
        await service.tick()
        await service.tick()
        assert bot.send_message.await_count == 1
        assert "Через 2 мин" in bot.send_message.call_args.kwargs["text"]
        now[0] = datetime(2026, 10, 1, 6, 1, tzinfo=timezone.utc)
        await service.tick()
        assert bot.send_message.await_count == 1
        db.close()

    asyncio.run(scenario())


def test_repeated_ping_attempts_do_not_extend_shared_timer_and_other_group_is_independent():
    async def scenario():
        db, bot, now, service = fixture()
        db.setup_group(-200, "Другая группа", None, "Europe/Moscow")
        person = db.observe(-200, 3, None, "Вторая группа")
        db.select(-200, person, True)
        assert (await service.ping(-100, "Первый")).startswith("Готово")
        until = db.group(-100)["cooldown_until"]
        for seconds in (1, 30, 60, 100, 100, 8):
            now[0] += timedelta(seconds=seconds)
            assert "Уже позвали" in await service.ping(-100, "Следующий")
            assert db.group(-100)["cooldown_until"] == until
        assert (await service.ping(-200, "Другая группа")).startswith("Готово")
        assert bot.send_message.await_count == 2
        now[0] += timedelta(seconds=1)
        assert (await service.ping(-100, "После регена")).startswith("Готово")
        assert bot.send_message.await_count == 3
        db.close()

    asyncio.run(scenario())


def test_auto_reminders_switch_between_week_templates():
    async def scenario():
        db, bot, now, service = fixture()
        db.clear_lessons(-100)
        db.add_lessons(
            -100,
            parse_lines(
                "Чт | 09:00-10:30 | Нечётная || нечетная\nЧт | 09:00-10:30 | Чётная || четная\nЧт | 09:00-10:30 | Общая"
            ),
        )
        db.set_week_cycle(-100, date(2026, 10, 1), "odd")
        await service.tick()
        texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
        assert (
            len(texts) == 2
            and any("Нечётная" in text for text in texts)
            and any("Общая" in text for text in texts)
        )
        assert not any("📚 Чётная" in text for text in texts)
        now[0] = datetime(2026, 10, 8, 5, 55, tzinfo=timezone.utc)
        bot.send_message.reset_mock()
        await service.tick()
        await service.tick()
        texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
        assert (
            len(texts) == 2
            and any("📚 Чётная" in text for text in texts)
            and any("Общая" in text for text in texts)
        )
        assert not any("Нечётная" in text for text in texts)
        db.close()

    asyncio.run(scenario())


def test_week_switch_before_midnight_reminder_uses_lesson_day():
    async def scenario():
        db, bot, now, service = fixture()
        db.clear_lessons(-100)
        db.add_lessons(
            -100, parse_lines("Пн | 00:02-01:00 | Нечётная || нечетная\nПн | 00:02-01:00 | Чётная || четная")
        )
        db.set_week_cycle(-100, date(2026, 10, 1), "odd")
        now[0] = datetime(2026, 10, 4, 20, 57, tzinfo=timezone.utc)
        await service.tick()
        assert bot.send_message.await_count == 1
        assert "📚 Чётная" in bot.send_message.call_args.kwargs["text"]
        db.close()

    asyncio.run(scenario())


def daily_fixture():
    db, bot, now, service = fixture()
    db.set_group(-100, tomorrow_enabled=1)
    now[0] = datetime(2026, 10, 1, 20, 30, tzinfo=timezone.utc)
    return db, bot, now, service


def test_daily_schedule_is_sent_once_at_selected_time_without_consuming_ping_cooldown():
    async def scenario():
        db, bot, now, service = daily_fixture()
        db.add_lessons(-100, parse_lines("Пт | 09:00-10:30 | Физика <&> | 202"))
        now[0] -= timedelta(seconds=1)
        await service.tick()
        bot.send_message.assert_not_awaited()
        now[0] += timedelta(seconds=1)
        await service.tick()
        await service.tick()
        assert bot.send_message.await_count == 1
        args = bot.send_message.call_args.kwargs
        assert args["chat_id"] == -100 and args["message_thread_id"] == 42
        assert args["disable_notification"] is False
        assert "Расписание на завтра" in args["text"] and "02.10.2026" in args["text"]
        assert "Физика &lt;&amp;&gt;" in args["text"] and "09:00–10:30" in args["text"]
        assert db.group(-100)["cooldown_until"] == 0
        assert db.rows("SELECT date,state FROM summary_deliveries") == [
            {"date": "2026-10-02", "state": "sent"}
        ]
        await Notifications(db, bot, lambda: now[0]).tick()
        assert bot.send_message.await_count == 1
        db.set_group(-100, tomorrow_time="23:31")
        now[0] += timedelta(minutes=1)
        await service.tick()
        assert bot.send_message.await_count == 1
        now[0] += timedelta(days=1)
        await service.tick()
        assert bot.send_message.await_count == 2
        db.close()

    asyncio.run(scenario())


def test_daily_schedule_resolves_week_cancellations_incoming_and_outgoing_moves():
    async def scenario():
        db, bot, now, service = daily_fixture()
        db.set_week_cycle(-100, date(2026, 10, 1), "odd")
        db.add_lessons(
            -100,
            parse_lines(
                "Пт | 09:00-10:30 | Нечётная || нечетная\nПт | 09:00-10:30 | Чётная || четная\nПт | 13:00-14:30 | Отменённая\nПт | 15:00-16:30 | На субботу"
            ),
        )
        target = date(2026, 10, 2)
        lessons = {item["name"]: item for item in db.lessons(-100)}
        db.cancel_occurrence(-100, lessons["Отменённая"]["id"], target)
        db.move_occurrence(
            -100, lessons["Матан <&>"]["id"], date(2026, 10, 1), target, "11:00", "12:30", "202"
        )
        db.move_occurrence(-100, lessons["На субботу"]["id"], target, date(2026, 10, 3), "15:00", "16:30")
        await service.tick()
        text = bot.send_message.call_args.kwargs["text"]
        assert "Нечётная неделя" in text and "<b>Нечётная</b>" in text
        assert "<b>Чётная</b>" not in text
        assert "Отменена" in text and "Отменённая" in text
        assert "Перенесена / изменена" in text and "11:00–12:30" in text and "202" in text
        assert "перенесена на 2026-10-03" in text
        db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancelled", [False, True])
def test_daily_empty_or_cancelled_day_is_announced_even_with_no_audience_and_class_reminders_paused(
    cancelled,
):
    async def scenario():
        db, bot, now, service = daily_fixture()
        db.set_group(-100, enabled=0)
        db.select_all(-100, False)
        if cancelled:
            db.set_day_cancelled(-100, date(2026, 10, 2), True)
        await service.tick()
        assert bot.send_message.await_count == 1
        text = bot.send_message.call_args.kwargs["text"]
        assert ("Все пары на этот день отменены" if cancelled else "Пар в расписании нет") in text
        assert "tg://user" not in text
        db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["disable", "time", "late", "cancel-day"])
def test_daily_schedule_rechecks_settings_time_and_schedule_after_waiting(change):
    async def scenario():
        db, bot, now, service = daily_fixture()
        db.add_lessons(-100, parse_lines("Пт | 09:00-10:30 | Завтрашняя"))
        await service.locks[-100].acquire()
        task = asyncio.create_task(service.tick())
        await asyncio.sleep(0)
        if change == "disable":
            db.set_group(-100, tomorrow_enabled=0)
        elif change == "time":
            db.set_group(-100, tomorrow_time="23:45")
        elif change == "late":
            now[0] += timedelta(minutes=2)
        else:
            db.set_day_cancelled(-100, date(2026, 10, 2), True)
        service.locks[-100].release()
        await task
        if change == "cancel-day":
            assert "Все пары на этот день отменены" in bot.send_message.call_args.kwargs["text"]
        else:
            bot.send_message.assert_not_awaited()
            assert not db.rows("SELECT * FROM summary_deliveries")
        db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error,state", [(NetworkError("timeout"), "uncertain"), (BadRequest("topic closed"), "failed")]
)
def test_daily_uncertain_or_rejected_delivery_is_not_duplicated(error, state):
    async def scenario():
        db, bot, now, service = daily_fixture()
        bot.send_message.side_effect = error
        await service.tick()
        await service.tick()
        assert bot.send_message.await_count == 1
        assert db.rows("SELECT state FROM summary_deliveries") == [{"state": state}]
        db.close()

    asyncio.run(scenario())


def test_daily_rate_limit_can_retry_safely_within_sending_window():
    async def scenario():
        db, bot, now, service = daily_fixture()
        bot.send_message.side_effect = [RetryAfter(timedelta(seconds=20)), SimpleNamespace(message_id=1)]
        await service.tick()
        assert not db.rows("SELECT * FROM summary_deliveries")
        await service.tick()
        assert bot.send_message.await_count == 1
        now[0] += timedelta(seconds=21)
        await service.tick()
        assert bot.send_message.await_count == 2
        assert db.rows("SELECT state FROM summary_deliveries") == [{"state": "sent"}]
        db.close()

    asyncio.run(scenario())


def test_long_daily_schedule_is_split_without_truncating_html_or_lessons():
    db, bot, now, service = daily_fixture()
    db.add_lessons(
        -100,
        parse_lines(
            "\n".join(f"Пт | 09:00-10:30 | Предмет {i} " + "&" * 160 + " | " + "&" * 230 for i in range(25))
        ),
    )
    chunks = tomorrow_chunks(db, db.group(-100), date(2026, 10, 2))
    assert len(chunks) > 1
    assert all(len(chunk) <= 3500 for chunk in chunks)
    for chunk in chunks:
        ElementTree.fromstring("<root>" + chunk + "</root>")
    assert sum(chunk.count("<b>Предмет") for chunk in chunks) == 25
    db.close()


def test_partial_daily_delivery_is_not_repeated(monkeypatch):
    async def scenario():
        db, bot, now, service = daily_fixture()
        monkeypatch.setattr("parabot.service.tomorrow_chunks", lambda *_: ["Часть 1", "Часть 2"])
        monkeypatch.setattr("parabot.service.asyncio.sleep", AsyncMock())
        bot.send_message.side_effect = [SimpleNamespace(message_id=1), NetworkError("timeout")]
        await service.tick()
        await service.tick()
        assert bot.send_message.await_count == 2
        assert db.rows("SELECT state FROM summary_deliveries") == [{"state": "uncertain"}]
        db.close()

    asyncio.run(scenario())


def test_daily_summary_has_independent_time_and_delivery_for_each_group():
    async def scenario():
        db, bot, now, service = daily_fixture()
        db.setup_group(-200, "Другая группа", 99, "UTC")
        db.set_group(-200, tomorrow_enabled=1, tomorrow_time="21:30")
        await service.tick()
        assert bot.send_message.await_count == 1
        assert bot.send_message.call_args.kwargs["chat_id"] == -100
        now[0] += timedelta(hours=1)
        await service.tick()
        assert bot.send_message.await_count == 2
        assert bot.send_message.call_args.kwargs["chat_id"] == -200
        assert bot.send_message.call_args.kwargs["message_thread_id"] == 99
        db.close()

    asyncio.run(scenario())
