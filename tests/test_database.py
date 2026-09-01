import tempfile
import unittest
from datetime import date
from pathlib import Path

from database import Repository, ScheduleEntry


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "test.db"
        self.repository = Repository(f"sqlite:///{database_path.as_posix()}")
        self.repository.create_schema()

    def tearDown(self):
        self.repository.engine.dispose()
        self.temp_dir.cleanup()

    def connect_group(self, group_id: int, code: str):
        return self.repository.consume_setup_code(
            code=code,
            group_id=group_id,
            group_title=f"Группа {group_id}",
            user_id=100,
            display_name="Владелец",
            timezone="Europe/Moscow",
            anchor_monday=date(2026, 8, 31),
            anchor_week_type="upper",
        )

    def test_setup_code_is_single_use_and_groups_are_isolated(self):
        code = self.repository.create_setup_code(100, "blank", 24)
        first = self.connect_group(-1001, code)
        second = self.connect_group(-1002, code)

        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual(self.repository.get_role(-1001, 100), "owner")
        self.assertIsNone(self.repository.get_role(-1002, 100))

    def test_new_code_cannot_take_over_an_existing_group(self):
        first_code = self.repository.create_setup_code(100, "blank", 24)
        self.assertTrue(self.connect_group(-1001, first_code).ok)
        second_code = self.repository.create_setup_code(100, "blank", 24)

        result = self.repository.consume_setup_code(
            code=second_code,
            group_id=-1001,
            group_title="Та же группа",
            user_id=200,
            display_name="Другой участник",
            timezone="Europe/Moscow",
            anchor_monday=date(2026, 8, 31),
            anchor_week_type="upper",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.message, "Эта группа уже настроена.")
        self.assertEqual(self.repository.get_role(-1001, 100), "owner")
        self.assertIsNone(self.repository.get_role(-1001, 200))

    def test_schedule_entries_do_not_leak_between_groups(self):
        for group_id in (-1001, -1002):
            code = self.repository.create_setup_code(100, "blank", 24)
            self.assertTrue(self.connect_group(group_id, code).ok)

        self.repository.add_schedule_entry(-1001, "upper", 0, "1. Математика")
        self.repository.add_schedule_entry(-1002, "upper", 0, "2. Физика")

        group_one = self.repository.list_schedule(-1001, "upper", 0)
        group_two = self.repository.list_schedule(-1002, "upper", 0)
        self.assertEqual([entry.text for entry in group_one], ["Математика"])
        self.assertEqual([entry.text for entry in group_two], ["Физика"])

    def test_existing_numbered_entries_are_normalized_on_startup(self):
        with self.repository.Session.begin() as session:
            session.add(
                ScheduleEntry(
                    group_id=-1001,
                    week_type="upper",
                    day_of_week=0,
                    position=1,
                    text="1. 2. Старое занятие",
                )
            )

        changed = self.repository.normalize_existing_schedule_entries()

        entries = self.repository.list_schedule(-1001, "upper", 0)
        self.assertEqual(changed, 1)
        self.assertEqual(entries[0].text, "Старое занятие")

    def test_moderator_role_is_scoped_to_one_group(self):
        for group_id in (-1001, -1002):
            code = self.repository.create_setup_code(100, "blank", 24)
            self.assertTrue(self.connect_group(group_id, code).ok)

        self.repository.add_moderator(-1001, 200, "Редактор", 100)
        self.assertEqual(self.repository.get_role(-1001, 200), "moderator")
        self.assertIsNone(self.repository.get_role(-1002, 200))

    def test_ownership_can_be_transferred(self):
        code = self.repository.create_setup_code(100, "blank", 24)
        self.assertTrue(self.connect_group(-1001, code).ok)
        self.repository.add_moderator(-1001, 200, "Новый владелец", 100)

        transferred = self.repository.transfer_ownership(
            -1001, 200, "Новый владелец", 100
        )

        self.assertTrue(transferred)
        self.assertEqual(self.repository.get_role(-1001, 200), "owner")
        self.assertEqual(self.repository.get_role(-1001, 100), "moderator")

    def test_subgroup_schedule_and_preference_are_isolated(self):
        code = self.repository.create_setup_code(100, "blank", 24)
        self.assertTrue(self.connect_group(-1001, code).ok)
        first = self.repository.add_subgroup(-1001, "Подгруппа 1")
        second = self.repository.add_subgroup(-1001, "Подгруппа 2")
        self.repository.add_schedule_entry(
            -1001, "upper", 0, "Общая лекция", subgroup_id=None
        )
        self.repository.add_schedule_entry(
            -1001, "upper", 0, "Лабораторная 1", subgroup_id=first.id
        )
        self.repository.add_schedule_entry(
            -1001, "upper", 0, "Лабораторная 2", subgroup_id=second.id
        )
        self.assertTrue(self.repository.set_user_subgroup(-1001, 300, first.id))

        selected = self.repository.get_user_subgroup(-1001, 300)
        first_entries = self.repository.list_schedule(
            -1001, "upper", 0, subgroup_id=first.id
        )
        second_entries = self.repository.list_schedule(
            -1001, "upper", 0, subgroup_id=second.id
        )
        self.assertEqual(selected.id, first.id)
        self.assertEqual([entry.text for entry in first_entries], ["Лабораторная 1"])
        self.assertEqual([entry.text for entry in second_entries], ["Лабораторная 2"])

    def test_entries_can_be_moved_inside_their_scope(self):
        code = self.repository.create_setup_code(100, "blank", 24)
        self.assertTrue(self.connect_group(-1001, code).ok)
        first = self.repository.add_schedule_entry(-1001, "upper", 0, "Первая")
        second = self.repository.add_schedule_entry(-1001, "upper", 0, "Вторая")

        self.repository.move_schedule_entry(-1001, second.id, "up")

        entries = self.repository.list_schedule(-1001, "upper", 0)
        self.assertEqual([entry.text for entry in entries], ["Вторая", "Первая"])
        self.assertEqual(first.position, 1)

    def test_lesson_times_are_shared_by_linked_chats(self):
        setup_code = self.repository.create_setup_code(100, "blank", 24)
        self.assertTrue(self.connect_group(-1001, setup_code).ok)
        copy_code = self.repository.create_group_copy_code(-1001, 100, 24)
        self.assertTrue(
            self.repository.consume_group_copy_code(
                copy_code, -1002, "Второй чат", 200
            ).ok
        )

        self.repository.set_lesson_time(-1002, 1, "08:00", "09:30")

        source_times = self.repository.list_lesson_times(-1001)
        linked_times = self.repository.list_lesson_times(-1002)
        self.assertEqual(
            [(item.lesson_number, item.start_time, item.end_time) for item in source_times],
            [(1, "08:00", "09:30")],
        )
        self.assertEqual(
            [(item.lesson_number, item.start_time, item.end_time) for item in linked_times],
            [(1, "08:00", "09:30")],
        )

    def test_template_import_extracts_shared_times_from_legacy_text(self):
        code = self.repository.create_setup_code(100, "blank", 24)
        self.assertTrue(self.connect_group(-1001, code).ok)

        self.repository.replace_schedule_from_template(
            -1001,
            {
                "upper": {
                    "0": [
                        "1. Математика, ауд. 204 08:00-09:30",
                        "2. Физика 09:40–11:10",
                    ]
                }
            },
        )

        entries = self.repository.list_schedule(-1001, "upper", 0)
        times = self.repository.list_lesson_times(-1001)
        self.assertEqual(
            [entry.text for entry in entries],
            ["Математика, ауд. 204", "Физика"],
        )
        self.assertEqual(
            [
                (item.lesson_number, item.start_time, item.end_time)
                for item in times
            ],
            [(1, "08:00", "09:30"), (2, "09:40", "11:10")],
        )

    def test_legacy_migration_extracts_times_before_normalizing_entries(self):
        code = self.repository.create_setup_code(100, "blank", 24)
        self.assertTrue(self.connect_group(-1001, code).ok)
        with self.repository.Session.begin() as session:
            session.add_all(
                [
                    ScheduleEntry(
                        group_id=-1001,
                        week_type="upper",
                        day_of_week=0,
                        position=1,
                        text="Математика 08:00-09:30",
                    ),
                    ScheduleEntry(
                        group_id=-1001,
                        week_type="lower",
                        day_of_week=1,
                        position=1,
                        text="Физика 08:00–09:30",
                    ),
                ]
            )

        self.assertEqual(self.repository.migrate_legacy_lesson_times(), 1)
        self.repository.normalize_existing_schedule_entries()

        times = self.repository.list_lesson_times(-1001)
        upper = self.repository.list_schedule(-1001, "upper", 0)
        lower = self.repository.list_schedule(-1001, "lower", 1)
        self.assertEqual(
            [(item.lesson_number, item.start_time, item.end_time) for item in times],
            [(1, "08:00", "09:30")],
        )
        self.assertEqual([entry.text for entry in upper], ["Математика"])
        self.assertEqual([entry.text for entry in lower], ["Физика"])

    def test_lesson_time_validation_rejects_invalid_ranges(self):
        code = self.repository.create_setup_code(100, "blank", 24)
        self.assertTrue(self.connect_group(-1001, code).ok)

        with self.assertRaisesRegex(ValueError, "позже"):
            self.repository.set_lesson_time(-1001, 1, "10:00", "09:00")
        with self.assertRaisesRegex(ValueError, "от 1 до 8"):
            self.repository.set_lesson_time(-1001, 9, "10:00", "11:00")

    def test_notification_settings_and_delivery_claim(self):
        code = self.repository.create_setup_code(100, "blank", 24)
        self.assertTrue(self.connect_group(-1001, code).ok)
        settings = self.repository.update_notification_settings(
            -1001, enabled=True, notification_time="08:15"
        )

        self.assertTrue(settings.enabled)
        self.assertEqual(settings.notification_time, "08:15")
        self.assertTrue(self.repository.claim_notification(-1001, date(2026, 9, 1)))
        self.assertFalse(self.repository.claim_notification(-1001, date(2026, 9, 1)))

    def test_linked_groups_share_schedule_settings_roles_and_subgroups(self):
        setup_code = self.repository.create_setup_code(100, "blank", 24)
        self.assertTrue(self.connect_group(-1001, setup_code).ok)
        self.repository.add_moderator(-1001, 200, "Редактор", 100)
        subgroup = self.repository.add_subgroup(-1001, "Первая подгруппа")
        self.repository.add_schedule_entry(-1001, "upper", 0, "Математика")
        self.repository.update_notification_settings(
            -1001, enabled=True, notification_time="08:15"
        )

        copy_code = self.repository.create_group_copy_code(-1001, 100, 24)
        result = self.repository.consume_group_copy_code(
            copy_code, -1002, "Второй чат", 300
        )

        self.assertTrue(result.ok)
        self.assertEqual(self.repository.resolve_group_id(-1002), -1001)
        self.assertEqual(self.repository.get_role(-1002, 100), "owner")
        self.assertEqual(self.repository.get_role(-1002, 200), "moderator")
        self.assertIsNone(self.repository.get_role(-1002, 300))
        self.assertEqual(
            [entry.text for entry in self.repository.list_schedule(-1002, "upper", 0)],
            ["Математика"],
        )
        self.assertEqual(self.repository.list_subgroups(-1002)[0].id, subgroup.id)
        self.assertTrue(self.repository.get_notification_settings(-1002).enabled)

        self.repository.add_schedule_entry(-1002, "upper", 0, "Физика")
        self.repository.update_group(-1002, title="Общее расписание")
        self.repository.rename_subgroup(-1002, subgroup.id, "Подгруппа А")

        self.assertEqual(
            [entry.text for entry in self.repository.list_schedule(-1001, "upper", 0)],
            ["Математика", "Физика"],
        )
        self.assertEqual(self.repository.get_group(-1001).title, "Общее расписание")
        self.assertEqual(self.repository.get_group(-1002).title, "Общее расписание")
        self.assertEqual(self.repository.list_subgroups(-1001)[0].name, "Подгруппа А")
        self.assertEqual(len(self.repository.list_linked_groups(-1002)), 2)

    def test_copy_code_is_single_use_and_cannot_replace_configured_group(self):
        for group_id in (-1001, -1003):
            setup_code = self.repository.create_setup_code(100, "blank", 24)
            self.assertTrue(self.connect_group(group_id, setup_code).ok)

        first_code = self.repository.create_group_copy_code(-1001, 100, 24)
        self.assertTrue(
            self.repository.consume_group_copy_code(
                first_code, -1002, "Второй чат", 300
            ).ok
        )
        reused = self.repository.consume_group_copy_code(
            first_code, -1004, "Четвёртый чат", 300
        )

        second_code = self.repository.create_group_copy_code(-1001, 100, 24)
        configured = self.repository.consume_group_copy_code(
            second_code, -1003, "Уже настроен", 300
        )

        self.assertFalse(reused.ok)
        self.assertIn("уже использован", reused.message)
        self.assertFalse(configured.ok)
        self.assertIn("уже настроена", configured.message)
        self.assertEqual(self.repository.resolve_group_id(-1003), -1003)


if __name__ == "__main__":
    unittest.main()
