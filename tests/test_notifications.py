import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from database import Repository
from notifications import NotificationDispatcher


class FakeBot:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


class NotificationDispatcherTests(unittest.TestCase):
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
        self.repository.update_notification_settings(
            -1001, enabled=True, notification_time="07:30"
        )
        self.repository.add_schedule_entry(-1001, "upper", 1, "1. Математика")
        self.bot = FakeBot()
        self.dispatcher = NotificationDispatcher(self.bot, self.repository)

    def tearDown(self):
        self.repository.engine.dispose()
        self.temp_dir.cleanup()

    def test_sends_once_inside_the_delivery_window(self):
        now = datetime(2026, 9, 1, 4, 31, tzinfo=UTC)

        self.dispatcher.run_once(now)
        self.dispatcher.run_once(now)

        self.assertEqual(len(self.bot.messages), 1)
        self.assertEqual(self.bot.messages[0][0], -1001)
        self.assertIn("Математика", self.bot.messages[0][1])

    def test_does_not_send_outside_the_delivery_window(self):
        now = datetime(2026, 9, 1, 5, 0, tzinfo=UTC)

        self.dispatcher.run_once(now)

        self.assertEqual(self.bot.messages, [])

    def test_shared_schedule_is_sent_to_every_linked_chat(self):
        copy_code = self.repository.create_group_copy_code(-1001, 1, 24)
        result = self.repository.consume_group_copy_code(
            copy_code, -1002, "Второй чат", 2
        )
        self.assertTrue(result.ok)
        now = datetime(2026, 9, 1, 4, 31, tzinfo=UTC)

        self.dispatcher.run_once(now)
        self.dispatcher.run_once(now)

        self.assertEqual({chat_id for chat_id, _ in self.bot.messages}, {-1001, -1002})
        self.assertEqual(len(self.bot.messages), 2)
        self.assertTrue(all("Математика" in text for _, text in self.bot.messages))


if __name__ == "__main__":
    unittest.main()
