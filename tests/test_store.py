from datetime import date

import pytest

from parabot.db import Store
from parabot.schedule import parse_lines


def setup(path=":memory:"):
    db = Store(path)
    db.setup_group(-100, "Группа", 42, "Europe/Moscow")
    return db


def test_selected_vs_all_and_departure():
    db = setup()
    a = db.observe(-100, 1, "alice_test", "Алиса")
    db.observe(-100, 2, None, "Борис")
    assert db.audience(db.group(-100)) == []
    db.select(-100, a, True)
    assert [m["user_id"] for m in db.audience(db.group(-100))] == [1]
    db.set_group(-100, audience="all")
    assert len(db.audience(db.group(-100))) == 2
    db.observe(-100, 1, "alice_test", "Алиса", present=False)
    assert [m["user_id"] for m in db.audience(db.group(-100))] == [2]
    db.mute(-100, 2, True)
    assert not db.audience(db.group(-100))
    db.close()


def test_manual_tag_merges_with_numeric_id_and_keeps_selection():
    db = setup()
    db.add_username(-100, "@Alice_Test")
    db.observe(-100, 1, "alice_test", "Алиса")
    assert len(db.members(-100)) == 1
    assert db.members(-100)[0]["user_id"] == 1
    assert db.members(-100)[0]["selected"] == 1
    db.observe(-100, 1, None, "Алиса")
    assert db.members(-100)[0]["username"] is None
    assert db.members(-100)[0]["selected"] == 1
    db.close()


def test_reassigned_username_does_not_move_selected_id():
    db = setup()
    old = db.observe(-100, 1, "alice_test", "Алиса")
    db.select(-100, old, True)
    db.observe(-100, 2, "alice_test", "Новый владелец")
    audience = db.audience(db.group(-100))
    assert audience[0]["user_id"] == 1
    assert audience[0]["username"] is None
    assert not db.members(-100)[1]["selected"]
    db.close()


def test_cooldown_restart_and_boundary(tmp_path):
    path = tmp_path / "bot.sqlite3"
    db = setup(path)
    assert db.claim_ping(-100, 1000)
    assert not db.claim_ping(-100, 1000)
    db.close()
    db = Store(path)
    assert not db.claim_ping(-100, 1299.999)
    assert db.claim_ping(-100, 1300)
    db.close()


def test_two_connections_cannot_claim_same_cooldown(tmp_path):
    path = tmp_path / "bot.sqlite3"
    db1 = setup(path)
    db2 = Store(path)
    assert db1.claim_ping(-100, 1000)
    assert not db2.claim_ping(-100, 1000)
    db1.close()
    db2.close()


def test_repeated_setup_preserves_settings_and_lessons():
    db = setup()
    db.set_group(-100, audience="all", enabled=0)
    lessons = parse_lines("Пн | 9:00-10:00 | Матан")
    assert db.add_lessons(-100, lessons) == 1
    assert db.add_lessons(-100, lessons) == 0
    db.setup_group(-100, "Новое имя", 99, "UTC")
    assert db.group(-100)["thread_id"] == 99
    assert db.group(-100)["timezone"] == "Europe/Moscow"
    assert db.group(-100)["audience"] == "all"
    assert db.group(-100)["enabled"] == 0
    assert len(db.lessons(-100)) == 1
    db.close()


def test_delivery_deduplication_and_skipped_date(tmp_path):
    path = tmp_path / "bot.sqlite3"
    db = setup(path)
    db.add_lessons(-100, parse_lines("Пн | 9:00-10:00 | Матан"))
    lesson_id = db.lessons(-100)[0]["id"]
    day = date(2026, 10, 5)
    assert db.reserve_delivery(lesson_id, day)
    db.finish_delivery(lesson_id, day, "sent")
    db.toggle_skip(-100, day)
    db.close()
    db = Store(path)
    assert not db.reserve_delivery(lesson_id, day)
    assert db.skipped(-100, day)
    db.toggle_skip(-100, day)
    assert not db.skipped(-100, day)
    db.close()


def test_cancellation_is_dated_and_persists_after_restart(tmp_path):
    path = tmp_path / "dated.db"
    db = setup(path)
    db.add_lessons(-100, parse_lines("Пн | 09:00-10:30 | Математика"))
    original = db.lessons(-100)[0]
    day = date(2026, 10, 5)
    db.cancel_occurrence(-100, original["id"], day)
    db.close()
    db = Store(path)
    assert db.occurrences(-100, day) == []
    assert db.occurrences(-100, day, include_cancelled=True)[0]["cancelled"]
    assert len(db.occurrences(-100, date(2026, 10, 12))) == 1
    assert db.lessons(-100) == [original]
    db.restore_occurrence(-100, original["id"], day)
    assert len(db.occurrences(-100, day)) == 1
    db.close()


def test_move_to_another_day_cancel_and_restore(tmp_path):
    path = tmp_path / "move.db"
    db = setup(path)
    db.add_lessons(-100, parse_lines("Пн | 09:00-10:30 | Математика | 304"))
    lesson = db.lessons(-100)[0]
    source, target = date(2026, 10, 5), date(2026, 10, 6)
    db.move_occurrence(-100, lesson["id"], source, target, "11:00", "12:30", "201")
    db.close()
    db = Store(path)
    assert not db.occurrences(-100, source)
    moved = db.occurrences(-100, target)[0]
    assert (moved["source_date"], moved["start"], moved["room"]) == (source.isoformat(), "11:00", "201")
    assert db.changes_on(-100, source)[0]["on_date"] == target.isoformat()
    assert db.occurrences(-100, date(2026, 10, 12))[0]["start"] == "09:00"
    db.cancel_occurrence(-100, lesson["id"], source)
    assert not db.occurrences(-100, target)
    db.restore_occurrence(-100, lesson["id"], source)
    assert not db.occurrences(-100, target)
    assert db.occurrences(-100, source)[0]["room"] == "304"
    db.close()


def test_cancelled_day_blocks_new_and_incoming_lessons_and_preserves_individual_cancel():
    db = setup()
    db.add_lessons(-100, parse_lines("Пн | 09:00-10:30 | Первая\nВт | 11:00-12:30 | Вторая"))
    first, second = db.lessons(-100)
    monday, tuesday = date(2026, 10, 5), date(2026, 10, 6)
    db.move_occurrence(-100, first["id"], monday, tuesday, "09:00", "10:30")
    db.cancel_occurrence(-100, second["id"], tuesday)
    db.set_day_cancelled(-100, tuesday, True)
    db.add_lessons(-100, parse_lines("2026-10-06 | 13:00-14:30 | Дополнительная"))
    assert not db.occurrences(-100, tuesday)
    with pytest.raises(ValueError, match="отменены все пары"):
        db.move_occurrence(-100, first["id"], monday, tuesday, "10:00", "11:30")
    db.set_day_cancelled(-100, tuesday, False)
    assert {item["name"] for item in db.occurrences(-100, tuesday)} == {"Первая", "Дополнительная"}
    assert db.occurrences(-100, date(2026, 10, 13))[0]["name"] == "Вторая"
    db.close()


def test_rescheduling_same_time_keeps_delivery_but_new_time_resets_it():
    db = setup()
    db.add_lessons(-100, parse_lines("Пн | 09:00-10:30 | Математика"))
    lesson_id = db.lessons(-100)[0]["id"]
    day = date(2026, 10, 5)
    db.reserve_delivery(lesson_id, day)
    db.finish_delivery(lesson_id, day, "sent")
    db.move_occurrence(-100, lesson_id, day, day, "09:00", "10:30")
    assert not db.reserve_delivery(lesson_id, day)
    db.move_occurrence(-100, lesson_id, day, day, "11:00", "12:30")
    assert db.reserve_delivery(lesson_id, day)
    db.close()


def test_edit_preserves_identity_and_rejects_duplicate_or_other_group():
    db = setup()
    db.setup_group(-200, "Другая группа", None, "UTC")
    db.add_lessons(-100, parse_lines("Пн | 09:00-10:30 | Математика\nВт | 11:00-12:30 | Физика"))
    first, second = db.lessons(-100)
    with pytest.raises(ValueError, match="уже есть"):
        db.update_lesson(-100, first["id"], parse_lines("Вт | 11:00-12:30 | Физика")[0])
    assert db.lesson(-100, first["id"]) == first
    with pytest.raises(ValueError):
        db.cancel_occurrence(-200, first["id"], date(2026, 10, 5))
    with pytest.raises(ValueError):
        db.update_lesson(-200, first["id"], parse_lines("Пн | 10:00-11:30 | Новая")[0])
    db.update_lesson(-100, first["id"], parse_lines("Ср | 10:00-11:30 | Новая | 202 | четная")[0])
    assert db.lesson(-100, first["id"])["day"] == 2
    assert db.lesson(-100, second["id"]) == second
    assert not db.occurrences(-100, date(2026, 10, 5))
    db.close()


def test_two_instances_of_same_weekly_lesson_can_move_to_same_day():
    db = setup()
    db.add_lessons(-100, parse_lines("Пн | 09:00-10:30 | Математика"))
    lesson_id = db.lessons(-100)[0]["id"]
    source, target = date(2026, 10, 5), date(2026, 10, 12)
    db.move_occurrence(-100, lesson_id, source, target, "11:00", "12:30")
    instances = db.occurrences(-100, target)
    assert len(instances) == 2
    assert {item["source_date"] for item in instances} == {source.isoformat(), target.isoformat()}
    db.close()


def test_reopening_legacy_database_adds_changes_without_losing_data(tmp_path):
    path = tmp_path / "legacy.db"
    db = setup(path)
    db.add_lessons(-100, parse_lines("Пн | 09:00-10:30 | Математика"))
    original = db.lessons(-100)
    db.write("DROP TABLE lesson_changes")
    db.write("DROP TABLE cancelled_dates")
    db.close()
    db = Store(path)
    assert db.lessons(-100) == original
    assert db.group(-100)["thread_id"] == 42
    db.cancel_occurrence(-100, original[0]["id"], date(2026, 10, 5))
    assert not db.occurrences(-100, date(2026, 10, 5))
    db.close()


def test_cancelled_day_survives_restart_and_deleted_lesson_removes_its_changes(tmp_path):
    path = tmp_path / "day-off.db"
    db = setup(path)
    db.add_lessons(-100, parse_lines("Пн | 09:00-10:30 | Математика"))
    lesson_id = db.lessons(-100)[0]["id"]
    day = date(2026, 10, 5)
    db.cancel_occurrence(-100, lesson_id, day)
    db.set_day_cancelled(-100, day, True)
    db.close()
    db = Store(path)
    assert db.day_cancelled(-100, day)
    assert db.changes_on(-100, day)
    db.delete_lesson(-100, lesson_id)
    assert not db.changes_on(-100, day)
    assert db.day_cancelled(-100, day)
    db.close()


def test_academic_cycle_persists_and_is_independent_for_each_group(tmp_path):
    path = tmp_path / "weeks.db"
    db = setup(path)
    db.setup_group(-200, "Другая группа", None, "Europe/Moscow")
    lessons = parse_lines(
        "Пн | 09:00-10:30 | Нечётная || нечетная\nПн | 09:00-10:30 | Чётная || четная\nПн | 11:00-12:30 | Общая"
    )
    db.add_lessons(-100, lessons)
    db.add_lessons(-200, lessons)
    db.set_week_cycle(-100, date(2026, 10, 1), "odd")  # Thursday sets the containing Monday
    db.set_week_cycle(-200, date(2026, 10, 1), "even")
    db.close()
    db = Store(path)
    assert db.group(-100)["week_anchor"] == "2026-09-28"
    assert {item["name"] for item in db.occurrences(-100, date(2026, 10, 5))} == {"Чётная", "Общая"}
    assert {item["name"] for item in db.occurrences(-200, date(2026, 10, 5))} == {"Нечётная", "Общая"}
    db.setup_group(-100, "Новое название", 99, "UTC")
    assert db.group(-100)["week_anchor_parity"] == "odd"
    db.cancel_occurrence(-100, db.lessons(-100)[0]["id"], date(2026, 10, 12))
    assert {item["name"] for item in db.occurrences(-100, date(2026, 10, 12))} == {"Общая"}
    db.close()


def test_legacy_database_week_migration_preserves_people_and_cooldown(tmp_path):
    path = tmp_path / "old-weeks.db"
    db = setup(path)
    person = db.observe(-100, 1, "student_one", "Алиса")
    db.select(-100, person, True)
    db.claim_ping(-100, 1000)
    db.add_lessons(-100, parse_lines("Пн | 09:00-10:30 | Матан || нечетная"))
    db.write("ALTER TABLE groups DROP COLUMN week_anchor")
    db.write("ALTER TABLE groups DROP COLUMN week_anchor_parity")
    db.close()
    db = Store(path)
    assert db.group(-100)["week_anchor"] == ""
    assert db.audience(db.group(-100))[0]["user_id"] == 1
    assert not db.claim_ping(-100, 1001)
    assert len(db.occurrences(-100, date(2026, 10, 5))) == 1
    db.set_week_cycle(-100, date(2026, 10, 5), "even")
    assert not db.occurrences(-100, date(2026, 10, 5))
    db.close()


def test_summary_settings_and_delivery_survive_restart_and_repeated_setup(tmp_path):
    path = tmp_path / "summary.db"
    db = setup(path)
    assert db.group(-100)["tomorrow_enabled"] == 0
    assert db.group(-100)["tomorrow_time"] == "23:30"
    db.set_group(-100, tomorrow_enabled=1, tomorrow_time="22:15")
    day = date(2026, 10, 2)
    assert db.reserve_summary(-100, day)
    db.finish_summary(-100, day, "sent")
    db.close()
    db = Store(path)
    db.setup_group(-100, "Новое имя", 99, "UTC")
    assert db.group(-100)["tomorrow_time"] == "22:15"
    assert db.group(-100)["tomorrow_enabled"] == 1
    assert not db.reserve_summary(-100, day)
    assert db.reserve_summary(-100, date(2026, 10, 3))
    db.close()


def test_summary_reservation_is_atomic_across_connections(tmp_path):
    path = tmp_path / "atomic-summary.db"
    first = setup(path)
    second = Store(path)
    day = date(2026, 10, 2)
    assert first.reserve_summary(-100, day)
    assert not second.reserve_summary(-100, day)
    first.release_summary(-100, day)
    assert second.reserve_summary(-100, day)
    first.close()
    second.close()


def test_legacy_summary_migration_preserves_week_settings_and_schedule(tmp_path):
    path = tmp_path / "legacy-summary.db"
    db = setup(path)
    db.add_lessons(-100, parse_lines("Пн | 09:00-10:30 | Математика || четная"))
    db.set_week_cycle(-100, date(2026, 10, 5), "even")
    db.write("ALTER TABLE groups DROP COLUMN tomorrow_enabled")
    db.write("ALTER TABLE groups DROP COLUMN tomorrow_time")
    db.write("DROP TABLE summary_deliveries")
    db.close()
    db = Store(path)
    assert db.group(-100)["tomorrow_enabled"] == 0
    assert db.group(-100)["week_anchor_parity"] == "even"
    assert db.occurrences(-100, date(2026, 10, 5))[0]["name"] == "Математика"
    assert db.reserve_summary(-100, date(2026, 10, 2))
    db.close()
