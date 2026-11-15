from __future__ import annotations


def register_commands(bot, relay_getter, owner_ids):
    """Register before the schedule bot's catch-all text handler."""
    def test_latest(message):
        user = getattr(message, "from_user", None)
        if user is None or user.id not in owner_ids:
            bot.reply_to(message, "Команда /vk_test доступна только владельцу бота.")
            return
        relay = relay_getter()
        if relay is None:
            bot.reply_to(message, "Пересылка ВК ещё не настроена или выключена.")
            return
        if message.chat.type != "private" and message.chat.id != relay.settings.target_chat:
            bot.reply_to(message, "Отправь /vk_test в целевой группе Telegram или в личном чате с ботом.")
            return
        if relay.request_test(message.chat.id):
            bot.reply_to(message, "Проверяю последнее сообщение ВК. Результат придёт в настроенную группу Telegram.")
        else:
            bot.reply_to(message, "Проверка уже запрошена. Дождись результата.")

    bot.register_message_handler(test_latest, commands=["vk_test"])
