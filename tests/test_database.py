import tempfile
import unittest
from datetime import date
from pathlib import Path

from database import Repository


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

    def test_schedule_entries_do_not_leak_between_groups(self):
        for group_id in (-1001, -1002):
            code = self.repository.create_setup_code(100, "blank", 24)
            self.assertTrue(self.connect_group(group_id, code).ok)

        self.repository.add_schedule_entry(-1001, "upper", 0, "1. Математика")
        self.repository.add_schedule_entry(-1002, "upper", 0, "1. Физика")

        group_one = self.repository.list_schedule(-1001, "upper", 0)
        group_two = self.repository.list_schedule(-1002, "upper", 0)
        self.assertEqual([entry.text for entry in group_one], ["1. Математика"])
        self.assertEqual([entry.text for entry in group_two], ["1. Физика"])

    def test_moderator_role_is_scoped_to_one_group(self):
        for group_id in (-1001, -1002):
            code = self.repository.create_setup_code(100, "blank", 24)
            self.assertTrue(self.connect_group(group_id, code).ok)

        self.repository.add_moderator(-1001, 200, "Редактор", 100)
        self.assertEqual(self.repository.get_role(-1001, 200), "moderator")
        self.assertIsNone(self.repository.get_role(-1002, 200))


if __name__ == "__main__":
    unittest.main()
