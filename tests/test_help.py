from types import SimpleNamespace
from unittest import TestCase

from config import Settings
from handlers import BotHandlers


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


class FakeRepository:
    def __init__(self, group=None, roles=None):
        self.group = group
        self.roles = roles or {}
        self.consumed_setup = None
        self.created_copy = None
        self.consumed_copy = None

    def get_group(self, group_id):
        return self.group

    def get_role(self, group_id, user_id):
        return self.roles.get(user_id)

    def list_subgroups(self, group_id):
        return []

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

    def test_participant_does_not_see_management_commands(self):
        bot = FakeBot()
        group = SimpleNamespace(title="ИКБО-01")
        handlers = BotHandlers(bot, FakeRepository(group), make_settings())

        handlers.help_command(make_message("supergroup"))

        text = bot.messages[-1][1]
        self.assertIn("Выбрать подгруппу", text)
        self.assertNotIn("/settings", text)
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
