import asyncio
import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError

from .messages import notification_chunks, panel_keyboard, tomorrow_chunks
from .schedule import due_occurrence, due_summary

log = logging.getLogger(__name__)
COOLDOWN = 300


class Notifications:
    def __init__(self, store, bot, clock=None):
        self.store, self.bot = store, bot
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.locks = defaultdict(asyncio.Lock)
        self.panel_locked = {}
        self.backoff = {}

    async def send_chunks(self, group, chunks):
        sent = 0
        for index, text in enumerate(chunks):
            if index:
                await asyncio.sleep(3.1)
            await self.bot.send_message(
                chat_id=group["chat_id"],
                message_thread_id=group["thread_id"],
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                disable_notification=False,
            )
            sent += 1
        return sent

    async def refresh_panel(self, chat_id):
        group = self.store.group(chat_id)
        if not group or not group["panel_id"]:
            return
        key = (chat_id, group["panel_id"])
        now = self.clock().timestamp()
        locked = group["cooldown_until"] > now
        if self.panel_locked.get(key) == locked:
            return
        try:
            await self.bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=group["panel_id"], reply_markup=panel_keyboard(group, now)
            )
            self.panel_locked[key] = locked
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                self.panel_locked[key] = locked
            elif "not found" in str(exc).lower():
                self.store.set_group(chat_id, panel_id=None)
            else:
                log.warning("Не удалось обновить панель группы %s: %s", chat_id, type(exc).__name__)
        except TelegramError as exc:
            log.warning("Не удалось обновить панель группы %s: %s", chat_id, type(exc).__name__)

    async def ping(self, chat_id, requester):
        async with self.locks[chat_id]:
            group = self.store.group(chat_id)
            if not group:
                return "Администратор ещё не настроил группу: /setup."
            members = self.store.audience(group)
            if not members:
                return "Список получателей пуст. Администратор может выбрать людей в /admin."
            now = self.clock().timestamp()
            if not self.store.claim_ping(chat_id, now, COOLDOWN):
                remaining = math.ceil(max(0, self.store.group(chat_id)["cooldown_until"] - now))
                return f"Уже позвали! Повторить можно через {remaining // 60}:{remaining % 60:02d}."
            header = "📣 <b>Отмечаемся!</b>\nПроверьте, не нужно ли отметиться на паре 👇"
            footer = f"Позвал(а): {escape(requester[:160])}. Следующий вызов — через 5 минут."
            # The reservation precedes network I/O; a parallel callback cannot bypass it.
            chunks = notification_chunks(header, members, footer)
            try:
                await self.send_chunks(group, chunks)
            except (Forbidden, BadRequest) as exc:
                # Only a single, definitely rejected message can safely release the lock.
                if len(chunks) == 1:
                    self.store.release_ping(chat_id, now + COOLDOWN)
                log.warning("Ручной вызов не доставлен в %s: %s", chat_id, type(exc).__name__)
                return "Telegram отклонил отправку. Проверьте права бота и открытую тему; ошибка записана в журнал."
            except RetryAfter:
                return "Telegram ограничил частоту отправки. Подождите 5 минут; повторный вызов временно заблокирован."
            except NetworkError:
                return "Сбой связи: результат отправки неизвестен. Во избежание повтора блокировка сохранена на 5 минут."
            finally:
                await self.refresh_panel(chat_id)
            return "Готово — участников позвали. Кнопка заблокирована на 5 минут."

    async def send_tomorrow(self, chat_id):
        group = self.store.group(chat_id)
        if not group or not due_summary(group, self.clock()):
            return
        async with self.locks[chat_id]:
            # Settings, calendar and schedule may change while a manual call holds the lock.
            group = self.store.group(chat_id)
            checked_at = self.clock()
            day = due_summary(group, checked_at) if group else None
            if not day or self.backoff.get(chat_id, 0) > checked_at.timestamp():
                return
            chunks = tomorrow_chunks(self.store, group, day)
            if not self.store.reserve_summary(chat_id, day):
                return
            try:
                await self.send_chunks(group, chunks)
            except RetryAfter as exc:
                wait = exc.retry_after
                seconds = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
                self.backoff[chat_id] = checked_at.timestamp() + seconds
                if len(chunks) == 1:
                    self.store.release_summary(chat_id, day)
                else:
                    self.store.finish_summary(chat_id, day, "uncertain")
                log.warning("Лимит Telegram при отправке расписания группы %s", chat_id)
            except (Forbidden, BadRequest) as exc:
                self.store.finish_summary(chat_id, day, "failed")
                log.warning("Расписание группы %s отклонено: %s", chat_id, type(exc).__name__)
            except NetworkError:
                self.store.finish_summary(chat_id, day, "uncertain")
                log.warning("Доставка расписания группы %s неизвестна; автоматического повтора нет", chat_id)
            except TelegramError as exc:
                self.store.finish_summary(chat_id, day, "failed")
                log.warning("Расписание группы %s отклонено: %s", chat_id, type(exc).__name__)
            else:
                self.store.finish_summary(chat_id, day, "sent")

    async def tick(self, context=None):
        now = self.clock()
        for group in self.store.groups():
            try:
                await self.refresh_panel(group["chat_id"])
                if self.backoff.get(group["chat_id"], 0) > now.timestamp():
                    continue
                await self.send_tomorrow(group["chat_id"])
                group = self.store.group(group["chat_id"])
                if (
                    not group
                    or not group["enabled"]
                    or self.backoff.get(group["chat_id"], 0) > self.clock().timestamp()
                ):
                    continue
                local_day = now.astimezone(ZoneInfo(group["timezone"])).date()
                lessons = [
                    lesson
                    for day in (local_day, local_day + timedelta(days=1))
                    for lesson in self.store.occurrences(group["chat_id"], day)
                ]
                for lesson in lessons:
                    occurrence = due_occurrence(lesson, now, group["timezone"], group["before_minutes"])
                    if not occurrence:
                        continue
                    day, begins = occurrence
                    if self.store.skipped(group["chat_id"], day):
                        continue
                    async with self.locks[group["chat_id"]]:
                        fresh = self.store.group(group["chat_id"])
                        if not fresh["enabled"]:
                            continue
                        source_day = date.fromisoformat(lesson["source_date"])
                        lesson = self.store.occurrence(group["chat_id"], lesson["id"], source_day)
                        if not lesson or lesson["cancelled"] or lesson["day_cancelled"]:
                            continue
                        # A large manual call may have held the lock; don't send a stale reminder.
                        checked_at = self.clock()
                        fresh_occurrence = due_occurrence(
                            lesson, checked_at, fresh["timezone"], fresh["before_minutes"]
                        )
                        if not fresh_occurrence:
                            continue
                        day, begins = fresh_occurrence
                        if self.store.skipped(group["chat_id"], day):
                            continue
                        members = self.store.audience(fresh)
                        if not members or not self.store.reserve_delivery(lesson["id"], source_day):
                            continue
                        minutes = max(1, math.ceil((begins - checked_at).total_seconds() / 60))
                        header = (
                            f"🔔 <b>Через {minutes} мин — пара!</b>\n"
                            f"📚 {escape(lesson['name'])}\n"
                            f"🕒 {lesson['start']}–{lesson['end']}"
                        )
                        if lesson["room"]:
                            header += f"\n📍 {escape(lesson['room'])}"
                        chunks = notification_chunks(header, members, "Успеваем собраться ☕")
                        try:
                            await self.send_chunks(fresh, chunks)
                        except RetryAfter as exc:
                            wait = exc.retry_after
                            seconds = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
                            self.backoff[group["chat_id"]] = now.timestamp() + seconds
                            if len(chunks) == 1:
                                self.store.release_delivery(lesson["id"], source_day)
                            else:
                                self.store.finish_delivery(lesson["id"], source_day, "uncertain")
                            log.warning("Лимит Telegram в группе %s", group["chat_id"])
                            break
                        except (Forbidden, BadRequest) as exc:
                            self.store.finish_delivery(lesson["id"], source_day, "failed")
                            log.warning(
                                "Напоминание %s отклонено: %s (%s)",
                                lesson["id"],
                                type(exc).__name__,
                                str(exc)[:200],
                            )
                        except NetworkError:
                            self.store.finish_delivery(lesson["id"], source_day, "uncertain")
                            log.warning(
                                "Доставка напоминания %s неизвестна; автоматического повтора нет",
                                lesson["id"],
                            )
                        except TelegramError as exc:
                            self.store.finish_delivery(lesson["id"], source_day, "failed")
                            log.warning("Напоминание %s отклонено: %s", lesson["id"], type(exc).__name__)
                        else:
                            self.store.finish_delivery(lesson["id"], source_day, "sent")
            except Exception as exc:
                # No secrets or user message bodies in logs.
                log.error("Ошибка обработки расписания группы %s: %s", group["chat_id"], type(exc).__name__)
