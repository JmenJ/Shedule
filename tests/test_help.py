from types import SimpleNamespace
from unittest import TestCase
from datetime import date

from config import Settings
from handlers import BotHandlers, parse_lesson_time_range, parse_lesson_times_bulk


class FakeBot:
    def __init__(self, telegram_status="member"):
        self.telegram_status = telegram_status
        self.messages = []
        self.chat_member_checks = 0

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))

    def reply_to(self, message, text, **kwargs):
        self.messages.append((message.chat.id, text, kwargs))

    def get_chat_member(self, group_id, user_id):
        self.chat_member_checks += 1
        return SimpleNamespace(status=self.telegram_status)

    def edit_message_text(self, text, **kwargs):
        self.messages.append((kwargs["chat_id"], text, kwargs))

    def answer_callback_query(self, callback_id, *args, **kwargs):
        return None

    def get_me(self):
        return SimpleNamespace(username="test_schedule_bot")


class FakeRepository:
    def __init__(self, group=None, roles=None, subgroups=None):
        self.group = group
        self.roles = roles or {}
        self.subgroups = subgroups or []
        self.user_subgroups = {}
        self.consumed_setup = None
        self.created_copy = None
        self.consumed_copy = None

    def get_group(self, group_id):
        return self.group

    def get_role(self, group_id, user_id):
        return self.roles.get(user_id)

    def list_subgroups(self, group_id):
        return self.subgroups

    def get_user_subgroup(self, group_id, user_id):
        subgroup_id = self.user_subgroups.get(user_id)
        return next(
            (item for item in self.subgroups if item.id == subgroup_id), None
        )

    def get_subgroup(self, group_id, subgroup_id):
        return next((item for item in self.subgroups if item.id == subgroup_id), None)

    def set_user_subgroup(self, group_id, user_id, subgroup_id):
        if not any(item.id == subgroup_id for item in self.subgroups):
            return False
        self.user_subgroups[user_id] = subgroup_id
        return True

    def list_schedule(self, group_id, week_type, day_of_week, subgroup_id=None):
        return []

    def list_lesson_times(self, group_id):
        return []

    def get_notification_settings(self, group_id):
        return SimpleNamespace(enabled=False, notification_time="07:30")

    def list_linked_groups(self, group_id):
        return [self.group] if self.group is not None else []

    def consume_setup_code(self, **kwargs):
        self.consumed_setup = kwargs
        return SimpleNamespace(ok=True, template_key="blank")

    def create_group_copy_code(self, **kwargs):
        self.created_copy = kwargs
        return "COPYCODE12"

    def consume_group_copy_code(self, **kwargs):
        self.consumed_copy = kwargs
        return SimpleNamespace(ok=True)


def make_message(chat_type, user_id=100, text=None):
    return SimpleNamespace(
        chat=SimpleNamespace(
            id=-1001 if chat_type != "private" else user_id,
            type=chat_type,
            title="Тестовая группа",
        ),
        from_user=SimpleNamespace(
            id=user_id,
            first_name="Участник",
            last_name=None,
            username=None,
        ),
        text=text,
    )


def make_settings(owner_ids=frozenset({999})):
    return Settings(
        bot_token="test",
        owner_ids=owner_ids,
        database_url="sqlite://",
        webhook_url=None,
        webhook_secret=None,
        default_timezone="Europe/Moscow",
        setup_code_ttl_hours=24,
    )


def make_group(chat_id=-1001):
    return SimpleNamespace(
        chat_id=chat_id,
        title="ИКБО-01",
        timezone="Europe/Moscow",
        anchor_monday=date(2026, 8, 31),
        anchor_week_type="upper",
        welcome_text="Выберите день",
        empty_day_text="Пар нет",
    )


def make_callback(data, user_id=100):
    return SimpleNamespace(
        id="callback-1",
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            message_id=10,
            chat=SimpleNamespace(id=-1001, type="supergroup"),
        ),
    )


class HelpCommandTests(TestCase):
    def test_private_owner_gets_setup_code_instructions(self):
        bot = FakeBot()
        handlers = BotHandlers(bot, FakeRepository(), make_settings())

        handlers.help_command(make_message("private", user_id=999))

        text = bot.messages[-1][1]
        self.assertIn("/admin", text)
        self.assertIn("/setup КОД", text)

    def test_unconfigured_group_user_with_code_gets_setup_instructions(self):
        bot = FakeBot(telegram_status="member")
        handlers = BotHandlers(bot, FakeRepository(), make_settings())

        handlers.help_command(make_message("supergroup"))

        self.assertIn("/setup КОД", bot.messages[-1][1])
        self.assertIn("администратора Telegram", bot.messages[-1][1])
        self.assertEqual(bot.chat_member_checks, 0)

    def test_non_admin_with_valid_code_becomes_group_owner(self):
        bot = FakeBot(telegram_status="member")
        repository = FakeRepository()
        handlers = BotHandlers(bot, repository, make_settings())

        handlers.setup_group(
            make_message("supergroup", user_id=321, text="/setup VALIDCODE")
        )

        self.assertEqual(repository.consumed_setup["user_id"], 321)
        self.assertEqual(repository.consumed_setup["code"], "VALIDCODE")
        self.assertEqual(bot.chat_member_checks, 0)
        self.assertIn("Вы назначены владельцем", bot.messages[-1][1])

    def test_user_activates_personal_schedule_in_private_chat(self):
        bot = FakeBot()
        repository = FakeRepository()
        handlers = BotHandlers(bot, repository, make_settings())

        handlers.setup_group(
            make_message("private", user_id=321, text="/setup VALIDCODE")
        )

        self.assertEqual(repository.consumed_setup["group_id"], 321)
        self.assertEqual(repository.consumed_setup["user_id"], 321)
        self.assertIn("Личное расписание", repository.consumed_setup["group_title"])
        self.assertIn("активировано", bot.messages[-1][1])

    def test_private_invite_link_activates_schedule(self):
        bot = FakeBot()
        repository = FakeRepository()
        handlers = BotHandlers(bot, repository, make_settings())

        handlers.start(
            make_message("private", user_id=321, text="/start setup_VALIDCODE")
        )

        self.assertEqual(repository.consumed_setup["code"], "VALIDCODE")

    def test_configured_personal_schedule_opens_on_start(self):
        bot = FakeBot()
        group = make_group(chat_id=321)
        handlers = BotHandlers(
            bot, FakeRepository(group, roles={321: "owner"}), make_settings()
        )

        handlers.start(make_message("private", user_id=321, text="/start"))

        self.assertEqual(bot.messages[-1][1], group.welcome_text)

    def test_personal_schedule_owner_can_create_group_copy_code(self):
        bot = FakeBot()
        group = make_group(chat_id=321)
        repository = FakeRepository(group, roles={321: "owner"})
        handlers = BotHandlers(bot, repository, make_settings())

        handlers.copy_group(make_message("private", user_id=321, text="/copy"))

        self.assertEqual(repository.created_copy["group_id"], 321)
        self.assertIn("/copy COPYCODE12", bot.messages[-1][1])

    def test_personal_settings_offer_group_link_without_moderator_controls(self):
        bot = FakeBot()
        group = make_group(chat_id=321)
        repository = FakeRepository(group, roles={321: "owner"})
        handlers = BotHandlers(bot, repository, make_settings())

        handlers.settings_menu(make_message("private", user_id=321))

        markup = bot.messages[-1][2]["reply_markup"]
        labels = [button.text for row in markup.keyboard for button in row]
        self.assertIn("🔗 Подключить группу", labels)
        self.assertNotIn("🛡 Модераторы", labels)

    def test_participant_does_not_see_management_commands(self):
        bot = FakeBot()
        group = SimpleNamespace(title="ИКБО-01")
        handlers = BotHandlers(bot, FakeRepository(group), make_settings())

        handlers.help_command(make_message("supergroup"))

        text = bot.messages[-1][1]
        self.assertIn("один раз попросит указать подгруппу", text)
        self.assertIn("/settings", text)
        self.assertNotIn("/mods", text)

    def test_moderator_sees_editing_but_not_access_management(self):
        bot = FakeBot()
        group = SimpleNamespace(title="ИКБО-01")
        repository = FakeRepository(group, roles={100: "moderator"})
        handlers = BotHandlers(bot, repository, make_settings())

        handlers.help_command(make_message("supergroup"))

        text = bot.messages[-1][1]
        self.assertIn("/settings", text)
        self.assertNotIn("Управление доступом", text)

    def test_group_owner_sees_role_and_transfer_instructions(self):
        bot = FakeBot()
        group = SimpleNamespace(title="ИКБО-01")
        repository = FakeRepository(group, roles={100: "owner"})
        handlers = BotHandlers(bot, repository, make_settings())

        handlers.help_command(make_message("supergroup"))

        text = bot.messages[-1][1]
        self.assertIn("/mods", text)
        self.assertIn("передача владения", text)
        self.assertIn("/copy", text)

    def test_group_owner_can_create_copy_code(self):
        bot = FakeBot()
        group = SimpleNamespace(title="ИКБО-01")
        repository = FakeRepository(group, roles={100: "owner"})
        handlers = BotHandlers(bot, repository, make_settings())

        handlers.copy_group(make_message("supergroup", text="/copy"))

        self.assertEqual(repository.created_copy["group_id"], -1001)
        self.assertIn("/copy COPYCODE12", bot.messages[-1][1])

    def test_participant_cannot_create_copy_code(self):
        bot = FakeBot()
        group = SimpleNamespace(title="ИКБО-01")
        repository = FakeRepository(group)
        handlers = BotHandlers(bot, repository, make_settings())

        handlers.copy_group(make_message("supergroup", text="/copy"))

        self.assertIsNone(repository.created_copy)
        self.assertIn("только владелец", bot.messages[-1][1])

    def test_first_schedule_view_requests_subgroup_once(self):
        bot = FakeBot()
        subgroups = [
            SimpleNamespace(id=1, name="Подгруппа 1"),
            SimpleNamespace(id=2, name="Подгруппа 2"),
        ]
        repository = FakeRepository(make_group(), subgroups=subgroups)
        handlers = BotHandlers(bot, repository, make_settings())

        handlers.handle_view_callback(make_callback("view:tomorrow"))

        text = bot.messages[-1][1]
        markup = bot.messages[-1][2]["reply_markup"]
        callbacks = [button.callback_data for row in markup.keyboard for button in row]
        self.assertIn("Бот запомнит выбор", text)
        self.assertIn("view:sub:1:tomorrow", callbacks)
        self.assertNotIn(100, repository.user_subgroups)

    def test_subgroup_is_saved_and_schedule_keyboard_has_no_edit_button(self):
        bot = FakeBot()
        subgroups = [SimpleNamespace(id=1, name="Подгруппа 1")]
        repository = FakeRepository(make_group(), subgroups=subgroups)
        handlers = BotHandlers(bot, repository, make_settings())

        handlers.handle_view_callback(make_callback("view:sub:1:today"))

        self.assertEqual(repository.user_subgroups[100], 1)
        markup = bot.messages[-1][2]["reply_markup"]
        labels = [button.text for row in markup.keyboard for button in row]
        self.assertFalse(any("подгруп" in label.lower() for label in labels))
        self.assertFalse(any("настрой" in label.lower() for label in labels))

    def test_participant_changes_subgroup_in_personal_settings(self):
        bot = FakeBot()
        subgroups = [SimpleNamespace(id=1, name="Подгруппа 1")]
        repository = FakeRepository(make_group(), subgroups=subgroups)
        handlers = BotHandlers(bot, repository, make_settings())

        handlers.settings_menu(make_message("supergroup"))

        text = bot.messages[-1][1]
        markup = bot.messages[-1][2]["reply_markup"]
        labels = [button.text for row in markup.keyboard for button in row]
        self.assertIn("Личные настройки", text)
        self.assertIn("👤 Изменить подгруппу", labels)
        self.assertNotIn("📚 Редактировать расписание", labels)


class LessonTimeInputTests(TestCase):
    def test_single_time_accepts_common_dash_variants(self):
        self.assertEqual(
            parse_lesson_time_range("08:00–09:30"), ("08:00", "09:30")
        )

    def test_bulk_input_accepts_numbered_lines(self):
        self.assertEqual(
            parse_lesson_times_bulk(
                "1. 08:00-09:30\n2 09:40–11:10\n8) 18:00—19:30"
            ),
            {
                1: ("08:00", "09:30"),
                2: ("09:40", "11:10"),
                8: ("18:00", "19:30"),
            },
        )

    def test_bulk_input_rejects_duplicate_lesson_numbers(self):
        with self.assertRaisesRegex(ValueError, "дважды"):
            parse_lesson_times_bulk("1. 08:00-09:30\n1. 09:40-11:10")
