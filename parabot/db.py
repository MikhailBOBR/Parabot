import sqlite3
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from .schedule import make_lesson, occurs


class Store:
    """A single-process store. All writes finish before yielding to the event loop."""

    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS groups (
          chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL,
          thread_id INTEGER, timezone TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1, audience TEXT NOT NULL DEFAULT 'selected',
          before_minutes INTEGER NOT NULL DEFAULT 5, cooldown_until REAL NOT NULL DEFAULT 0,
          panel_id INTEGER, panel_thread INTEGER);
        CREATE TABLE IF NOT EXISTS members (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_id INTEGER NOT NULL REFERENCES groups ON DELETE CASCADE,
          user_id INTEGER, username TEXT, name TEXT NOT NULL,
          selected INTEGER NOT NULL DEFAULT 0, present INTEGER NOT NULL DEFAULT 1,
          muted INTEGER NOT NULL DEFAULT 0,
          UNIQUE(chat_id,user_id));
        CREATE INDEX IF NOT EXISTS members_username ON members(chat_id,username);
        CREATE TABLE IF NOT EXISTS lessons (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_id INTEGER NOT NULL REFERENCES groups ON DELETE CASCADE,
          day INTEGER NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
          name TEXT NOT NULL, room TEXT NOT NULL DEFAULT '',
          week TEXT NOT NULL DEFAULT 'every', on_date TEXT NOT NULL DEFAULT '',
          UNIQUE(chat_id,day,start,end,name,room,week,on_date));
        CREATE TABLE IF NOT EXISTS skipped_dates (
          chat_id INTEGER NOT NULL REFERENCES groups ON DELETE CASCADE,
          date TEXT NOT NULL, PRIMARY KEY(chat_id,date));
        CREATE TABLE IF NOT EXISTS deliveries (
          lesson_id INTEGER NOT NULL REFERENCES lessons ON DELETE CASCADE,
          date TEXT NOT NULL, state TEXT NOT NULL,
          PRIMARY KEY(lesson_id,date));
        CREATE TABLE IF NOT EXISTS cancelled_dates (
          chat_id INTEGER NOT NULL REFERENCES groups ON DELETE CASCADE,
          date TEXT NOT NULL, PRIMARY KEY(chat_id,date));
        CREATE TABLE IF NOT EXISTS lesson_changes (
          lesson_id INTEGER NOT NULL REFERENCES lessons ON DELETE CASCADE,
          date TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
          on_date TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
          name TEXT NOT NULL, room TEXT NOT NULL,
          PRIMARY KEY(lesson_id,date));
        CREATE INDEX IF NOT EXISTS changes_target ON lesson_changes(on_date);
        CREATE TABLE IF NOT EXISTS summary_deliveries (
          chat_id INTEGER NOT NULL REFERENCES groups ON DELETE CASCADE,
          date TEXT NOT NULL, state TEXT NOT NULL,
          PRIMARY KEY(chat_id,date));
        """)
        # Existing installations retain their ISO calendar until an admin sets an academic week.
        columns = {item["name"] for item in self.rows("PRAGMA table_info(groups)")}
        with self.conn:
            if "week_anchor" not in columns:
                self.conn.execute("ALTER TABLE groups ADD COLUMN week_anchor TEXT NOT NULL DEFAULT ''")
            if "week_anchor_parity" not in columns:
                self.conn.execute(
                    "ALTER TABLE groups ADD COLUMN week_anchor_parity TEXT NOT NULL DEFAULT 'odd'"
                )
            if "tomorrow_enabled" not in columns:
                self.conn.execute("ALTER TABLE groups ADD COLUMN tomorrow_enabled INTEGER NOT NULL DEFAULT 0")
            if "tomorrow_time" not in columns:
                self.conn.execute("ALTER TABLE groups ADD COLUMN tomorrow_time TEXT NOT NULL DEFAULT '23:30'")

    def close(self):
        self.conn.close()

    def row(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def rows(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args)]

    def write(self, sql, args=()):
        with self.conn:
            return self.conn.execute(sql, args)

    def setup_group(self, chat_id, title, thread_id, timezone):
        self.write(
            """INSERT INTO groups(chat_id,title,thread_id,timezone) VALUES(?,?,?,?)
                   ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,thread_id=excluded.thread_id""",
            (chat_id, title, thread_id, timezone),
        )

    def group(self, chat_id):
        return self.row("SELECT * FROM groups WHERE chat_id=?", (chat_id,))

    def groups(self):
        return self.rows("SELECT * FROM groups ORDER BY title")

    def set_group(self, chat_id, **values):
        allowed = {
            "title",
            "thread_id",
            "timezone",
            "enabled",
            "audience",
            "before_minutes",
            "panel_id",
            "panel_thread",
            "week_anchor",
            "week_anchor_parity",
            "tomorrow_enabled",
            "tomorrow_time",
        }
        if not values or not values.keys() <= allowed:
            raise ValueError("Недопустимая настройка.")
        columns = ",".join(f"{key}=?" for key in values)
        self.write(f"UPDATE groups SET {columns} WHERE chat_id=?", (*values.values(), chat_id))

    def set_week_cycle(self, chat_id, reference_day, parity):
        if parity not in ("odd", "even"):
            raise ValueError("Неделя должна быть чётной или нечётной.")
        monday = reference_day - timedelta(days=reference_day.weekday())
        self.set_group(chat_id, week_anchor=monday.isoformat(), week_anchor_parity=parity)

    def observe(self, chat_id, user_id, username, name, present=True):
        username = username.lower() if username else None
        with self.conn:
            existing = self.row("SELECT * FROM members WHERE chat_id=? AND user_id=?", (chat_id, user_id))
            if not existing and username:
                existing = self.row(
                    "SELECT * FROM members WHERE chat_id=? AND user_id IS NULL AND username=?",
                    (chat_id, username),
                )
            # Usernames can be changed/reassigned; stable numeric IDs stay attached to their owner.
            if username:
                self.conn.execute(
                    "UPDATE members SET username=NULL WHERE chat_id=? AND username=? AND user_id IS NOT NULL AND user_id<>?",
                    (chat_id, username, user_id),
                )
            if existing:
                self.conn.execute(
                    "UPDATE members SET user_id=?,username=?,name=?,present=? WHERE id=?",
                    (user_id, username, name[:160], int(present), existing["id"]),
                )
                return existing["id"]
            cursor = self.conn.execute(
                "INSERT INTO members(chat_id,user_id,username,name,present) VALUES(?,?,?,?,?)",
                (chat_id, user_id, username, name[:160], int(present)),
            )
            return cursor.lastrowid

    def add_username(self, chat_id, username):
        username = username.lstrip("@").lower()
        row = self.row(
            "SELECT * FROM members WHERE chat_id=? AND username=? ORDER BY user_id IS NULL LIMIT 1",
            (chat_id, username),
        )
        if row:
            self.write("UPDATE members SET selected=1,muted=0,present=1 WHERE id=?", (row["id"],))
        else:
            self.write(
                "INSERT INTO members(chat_id,username,name,selected) VALUES(?,?,?,1)",
                (chat_id, username, "@" + username),
            )

    def members(self, chat_id):
        return self.rows("SELECT * FROM members WHERE chat_id=? ORDER BY name COLLATE NOCASE,id", (chat_id,))

    def member(self, chat_id, member_id):
        return self.row("SELECT * FROM members WHERE chat_id=? AND id=?", (chat_id, member_id))

    def select(self, chat_id, member_id, selected):
        self.write(
            "UPDATE members SET selected=?,muted=0 WHERE chat_id=? AND id=?",
            (int(selected), chat_id, member_id),
        )

    def select_all(self, chat_id, selected):
        self.write(
            "UPDATE members SET selected=?,muted=CASE WHEN ?=1 THEN 0 ELSE muted END WHERE chat_id=? AND present=1",
            (int(selected), int(selected), chat_id),
        )

    def mute(self, chat_id, user_id, muted):
        self.write(
            "UPDATE members SET muted=?,selected=CASE WHEN ?=1 THEN 0 ELSE selected END WHERE chat_id=? AND user_id=?",
            (int(muted), int(muted), chat_id, user_id),
        )

    def remove_member(self, chat_id, member_id):
        self.write("DELETE FROM members WHERE chat_id=? AND id=?", (chat_id, member_id))

    def audience(self, group):
        clause = " AND selected=1" if group["audience"] == "selected" else ""
        return self.rows(
            "SELECT * FROM members WHERE chat_id=? AND present=1 AND muted=0" + clause + " ORDER BY id",
            (group["chat_id"],),
        )

    def lessons(self, chat_id):
        return self.rows("SELECT * FROM lessons WHERE chat_id=? ORDER BY on_date,day,start,id", (chat_id,))

    def lesson(self, chat_id, lesson_id):
        return self.row("SELECT * FROM lessons WHERE chat_id=? AND id=?", (chat_id, lesson_id))

    def update_lesson(self, chat_id, lesson_id, lesson):
        values = asdict(lesson)
        try:
            cursor = self.write(
                "UPDATE lessons SET day=?,start=?,end=?,name=?,room=?,week=?,on_date=? WHERE chat_id=? AND id=?",
                (*values.values(), chat_id, lesson_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Такая пара уже есть в расписании.") from exc
        if not cursor.rowcount:
            raise ValueError("Пара уже удалена. Откройте расписание заново.")

    def change(self, chat_id, lesson_id, source_day):
        return self.row(
            """SELECT c.* FROM lesson_changes c JOIN lessons l ON l.id=c.lesson_id
               WHERE l.chat_id=? AND c.lesson_id=? AND c.date=?""",
            (chat_id, lesson_id, source_day.isoformat()),
        )

    def occurrence(self, chat_id, lesson_id, source_day):
        """Resolve one instance, retaining its original date as a stable identity."""
        lesson = self.lesson(chat_id, lesson_id)
        if not lesson:
            return None
        change = self.change(chat_id, lesson_id, source_day)
        if not change and not occurs(lesson, source_day, self.group(chat_id)):
            return None
        result = {**lesson, "source_date": source_day.isoformat(), "changed": bool(change)}
        result["on_date"], result["week"] = source_day.isoformat(), "every"
        if change:
            result.update({key: change[key] for key in ("on_date", "start", "end", "name", "room")})
        target_day = date.fromisoformat(result["on_date"])
        result["day"] = target_day.weekday()
        result["cancelled"] = bool(change and change["cancelled"])
        result["day_cancelled"] = self.day_cancelled(chat_id, target_day)
        return result

    def occurrences(self, chat_id, day, include_cancelled=False):
        group = self.group(chat_id)
        candidates = {(item["id"], day) for item in self.lessons(chat_id) if occurs(item, day, group)}
        candidates.update(
            (item["lesson_id"], date.fromisoformat(item["date"]))
            for item in self.rows(
                """SELECT c.lesson_id,c.date FROM lesson_changes c JOIN lessons l ON l.id=c.lesson_id
                   WHERE l.chat_id=? AND c.on_date=?""",
                (chat_id, day.isoformat()),
            )
        )
        result = []
        for lesson_id, source_day in candidates:
            item = self.occurrence(chat_id, lesson_id, source_day)
            if item and item["on_date"] == day.isoformat():
                if include_cancelled or not (item["cancelled"] or item["day_cancelled"]):
                    result.append(item)
        return sorted(result, key=lambda item: (item["start"], item["id"], item["source_date"]))

    def day_cancelled(self, chat_id, day):
        return bool(
            self.row("SELECT 1 FROM cancelled_dates WHERE chat_id=? AND date=?", (chat_id, day.isoformat()))
        )

    def set_day_cancelled(self, chat_id, day, cancelled):
        if cancelled:
            self.write("INSERT OR IGNORE INTO cancelled_dates VALUES(?,?)", (chat_id, day.isoformat()))
        else:
            self.write("DELETE FROM cancelled_dates WHERE chat_id=? AND date=?", (chat_id, day.isoformat()))

    def cancel_occurrence(self, chat_id, lesson_id, source_day):
        item = self.occurrence(chat_id, lesson_id, source_day)
        if not item:
            raise ValueError("Пара уже удалена или отсутствует на выбранную дату.")
        self.write(
            """INSERT INTO lesson_changes(lesson_id,date,cancelled,on_date,start,end,name,room)
               VALUES(?,?,1,?,?,?,?,?) ON CONFLICT(lesson_id,date) DO UPDATE SET cancelled=1""",
            (
                lesson_id,
                source_day.isoformat(),
                item["on_date"],
                item["start"],
                item["end"],
                item["name"],
                item["room"],
            ),
        )

    def move_occurrence(self, chat_id, lesson_id, source_day, target_day, start, end, room=None):
        item = self.occurrence(chat_id, lesson_id, source_day)
        if not item:
            raise ValueError("Пара уже удалена или отсутствует на выбранную дату.")
        replacement = make_lesson(
            target_day.isoformat(), start, end, item["name"], item["room"] if room is None else room
        )
        if self.day_cancelled(chat_id, target_day):
            raise ValueError(
                "На эту дату отменены все пары. Сначала восстановите день или выберите другую дату."
            )
        unchanged = not item["cancelled"] and all(
            item[key] == getattr(replacement, key) for key in ("on_date", "start", "end", "name", "room")
        )
        if unchanged:
            return
        with self.conn:
            self.conn.execute(
                """INSERT INTO lesson_changes(lesson_id,date,cancelled,on_date,start,end,name,room)
                   VALUES(?,?,0,?,?,?,?,?) ON CONFLICT(lesson_id,date) DO UPDATE SET
                   cancelled=0,on_date=excluded.on_date,start=excluded.start,end=excluded.end,
                   name=excluded.name,room=excluded.room""",
                (
                    lesson_id,
                    source_day.isoformat(),
                    replacement.on_date,
                    replacement.start,
                    replacement.end,
                    replacement.name,
                    replacement.room,
                ),
            )
            # An intentional reschedule gets a reminder at its new time, even if the old one was sent.
            self.conn.execute(
                "DELETE FROM deliveries WHERE lesson_id=? AND date=?", (lesson_id, source_day.isoformat())
            )

    def restore_occurrence(self, chat_id, lesson_id, source_day):
        if not self.lesson(chat_id, lesson_id):
            raise ValueError("Пара уже удалена.")
        self.write(
            "DELETE FROM lesson_changes WHERE lesson_id=? AND date=?", (lesson_id, source_day.isoformat())
        )

    def changes_on(self, chat_id, day):
        """Include outgoing moves so that their original date still offers undo."""
        return self.rows(
            """SELECT c.*,l.day,l.week,l.on_date AS original_on_date FROM lesson_changes c
               JOIN lessons l ON l.id=c.lesson_id WHERE l.chat_id=? AND (c.date=? OR c.on_date=?)
               ORDER BY c.start,c.lesson_id,c.date""",
            (chat_id, day.isoformat(), day.isoformat()),
        )

    def add_lessons(self, chat_id, lessons):
        count = 0
        with self.conn:
            for lesson in lessons:
                values = asdict(lesson)
                cursor = self.conn.execute(
                    """INSERT OR IGNORE INTO lessons(chat_id,day,start,end,name,room,week,on_date)
                                            VALUES(?,?,?,?,?,?,?,?)""",
                    (chat_id, *values.values()),
                )
                count += cursor.rowcount
        return count

    def delete_lesson(self, chat_id, lesson_id):
        self.write("DELETE FROM lessons WHERE chat_id=? AND id=?", (chat_id, lesson_id))

    def clear_lessons(self, chat_id):
        self.write("DELETE FROM lessons WHERE chat_id=?", (chat_id,))

    def skipped(self, chat_id, day):
        return bool(
            self.row("SELECT 1 FROM skipped_dates WHERE chat_id=? AND date=?", (chat_id, day.isoformat()))
        )

    def toggle_skip(self, chat_id, day):
        if self.skipped(chat_id, day):
            self.write("DELETE FROM skipped_dates WHERE chat_id=? AND date=?", (chat_id, day.isoformat()))
        else:
            self.write("INSERT INTO skipped_dates VALUES(?,?)", (chat_id, day.isoformat()))

    def claim_ping(self, chat_id, now, seconds=300):
        """Atomic shared cooldown, durable across restart and different messages/commands."""
        cursor = self.write(
            "UPDATE groups SET cooldown_until=? WHERE chat_id=? AND cooldown_until<=?",
            (now + seconds, chat_id, now),
        )
        return cursor.rowcount == 1

    def release_ping(self, chat_id, claimed_until):
        self.write(
            "UPDATE groups SET cooldown_until=0 WHERE chat_id=? AND cooldown_until=?",
            (chat_id, claimed_until),
        )

    def reserve_delivery(self, lesson_id, day):
        cursor = self.write(
            "INSERT OR IGNORE INTO deliveries VALUES(?,?,'sending')", (lesson_id, day.isoformat())
        )
        return cursor.rowcount == 1

    def finish_delivery(self, lesson_id, day, state):
        self.write(
            "UPDATE deliveries SET state=? WHERE lesson_id=? AND date=?", (state, lesson_id, day.isoformat())
        )

    def release_delivery(self, lesson_id, day):
        self.write(
            "DELETE FROM deliveries WHERE lesson_id=? AND date=? AND state='sending'",
            (lesson_id, day.isoformat()),
        )

    def reserve_summary(self, chat_id, day):
        cursor = self.write(
            "INSERT OR IGNORE INTO summary_deliveries VALUES(?,?,'sending')", (chat_id, day.isoformat())
        )
        return cursor.rowcount == 1

    def finish_summary(self, chat_id, day, state):
        self.write(
            "UPDATE summary_deliveries SET state=? WHERE chat_id=? AND date=?",
            (state, chat_id, day.isoformat()),
        )

    def release_summary(self, chat_id, day):
        self.write(
            "DELETE FROM summary_deliveries WHERE chat_id=? AND date=? AND state='sending'",
            (chat_id, day.isoformat()),
        )
