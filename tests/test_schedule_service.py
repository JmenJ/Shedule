import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from database import Repository
from schedule_service import format_schedule, target_date_for_action, week_type_for_date


class ScheduleServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "test.db"
        self.repository = Repository(f"sqlite:///{database_path.as_posix()}")
        self.repository.create_schema()
        code = self.repository.create_setup_code(1, "blank", 24)
        result = self.repository.consume_setup_code(
            code=code,
            group_id=-1001,
            group_title="Тест",
            user_id=1,
            display_name="Owner",
            timezone="Europe/Moscow",
            anchor_monday=date(2026, 8, 31),
            anchor_week_type="upper",
        )
        self.assertTrue(result.ok)
        self.group = self.repository.get_group(-1001)

    def tearDown(self):
        self.repository.engine.dispose()
        self.temp_dir.cleanup()

    def test_week_type_alternates_each_monday(self):
        self.assertEqual(week_type_for_date(self.group, date(2026, 9, 6)), "upper")
        self.assertEqual(week_type_for_date(self.group, date(2026, 9, 7)), "lower")
        self.assertEqual(week_type_for_date(self.group, date(2026, 9, 14)), "upper")

    def test_tomorrow_uses_group_timezone(self):
        now = datetime(2026, 8, 31, 22, 30, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(
            target_date_for_action(self.group, "tomorrow", now), date(2026, 9, 2)
        )

    def test_format_schedule_uses_group_entries(self):
        self.repository.add_schedule_entry(
            -1001, "upper", 0, "1. Математика 08:00–09:30"
        )
        text = format_schedule(self.repository, self.group, date(2026, 8, 31))
        self.assertIn("Верхняя неделя", text)
        self.assertIn("Математика", text)


if __name__ == "__main__":
    unittest.main()
