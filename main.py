from __future__ import annotations

import hmac
import logging
import os

import telebot
from flask import Flask, abort, jsonify, request
from telebot import types

from config import Settings
from database import Repository
from handlers import BotHandlers
from notifications import NotificationDispatcher

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger(__name__)

settings = Settings.from_environment()
repository = Repository(settings.database_url)
repository.create_schema()

bot = telebot.TeleBot(settings.bot_token, threaded=True)
handlers = BotHandlers(bot, repository, settings)
handlers.register()
notification_dispatcher = NotificationDispatcher(bot, repository)

app = Flask(__name__)


@app.get("/")
def healthcheck():
    return jsonify(status="ok", service="schedule-bot")


@app.post("/webhook")
def webhook():
    if settings.webhook_secret is None:
        abort(503)

    provided_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(provided_secret, settings.webhook_secret):
        abort(403)
    if not request.is_json:
        abort(415)

    update = types.Update.de_json(request.get_data(as_text=True))
    bot.process_new_updates([update])
    return "", 204


def configure_telegram() -> None:
    commands = [
        types.BotCommand("start", "открыть расписание"),
        types.BotCommand("help", "помощь с учётом ваших прав"),
        types.BotCommand("myid", "показать мой Telegram ID"),
        types.BotCommand("settings", "настройки этой группы"),
        types.BotCommand("mods", "редакторы этой группы"),
        types.BotCommand("copy", "объединить чаты одним расписанием"),
        types.BotCommand("cancel", "отменить ввод"),
    ]
    bot.set_my_commands(commands)

    if settings.webhook_url:
        webhook_target = f"{settings.webhook_url}/webhook"
        bot.set_webhook(
            url=webhook_target,
            secret_token=settings.webhook_secret,
            allowed_updates=["message", "callback_query"],
        )
        LOGGER.info("Webhook настроен: %s", webhook_target)
    else:
        bot.remove_webhook()
        LOGGER.info("WEBHOOK_URL не задан — запуск в режиме polling")


if __name__ == "__main__":
    configure_telegram()
    notification_dispatcher.start()
    try:
        if settings.webhook_url:
            port = int(os.environ.get("PORT", "10000"))
            app.run(host="0.0.0.0", port=port, threaded=True)
        else:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
    finally:
        notification_dispatcher.shutdown()
