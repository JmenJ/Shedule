from types import SimpleNamespace
from unittest import TestCase

from config import Settings
from handlers import BotHandlers


class FakeBot:
    def __init__(self, telegram_status="member"):
        self.telegram_status = telegram_status
        self.messages = []

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))

    def reply_to(self, message, text, **kwargs):
        self.messages.append((message.chat.id, text, kwargs))

    def get_chat_member(self, group_id, user_id):
        return SimpleNamespace(status=self.telegram_status)


class FakeRepository:
    def __init__(self, group=None, roles=None):
        self.group = group
        self.roles = roles or {}

    def get_group(self, group_id):
        return self.group

    def get_role(self, group_id, user_id):
        return self.roles.get(user_id)

    def list_subgroups(self, group_id):
        return []


def make_message(chat_type, user_id=100):
    return SimpleNamespace(
        chat=SimpleNamespace(
            id=-1001 if chat_type != "private" else user_id, type=chat_type
        ),
        from_user=SimpleNamespace(id=user_id),
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

    def test_unconfigured_group_admin_gets_setup_instructions(self):
        bot = FakeBot(telegram_status="administrator")
        handlers = BotHandlers(bot, FakeRepository(), make_settings())

        handlers.help_command(make_message("supergroup"))

        self.assertIn("/setup КОД", bot.messages[-1][1])
        self.assertIn("Вы администратор", bot.messages[-1][1])

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
