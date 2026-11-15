"""Standalone Render web service; no schedule database is opened."""
import hmac
import logging
import os
import threading

import telebot
from flask import Flask, abort, jsonify, request
from telebot import types

from config import Settings
from .commands import register_commands
from .service import RelaySettings, VkRelay


def create_web_app(bot, settings, relay):
    app = Flask(__name__)

    @app.get("/")
    def health():
        return jsonify(status="ok", service="vk-relay",
                       vk_relay=relay.status if relay else "disabled",
                       vk_transport=getattr(getattr(relay, "settings", None), "transport", None),
                       vk_last_read_seconds_ago=getattr(relay, "last_read_age", None))

    @app.post("/webhook")
    def webhook():
        if not settings.webhook_secret:
            abort(503)
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(secret, settings.webhook_secret):
            abort(403)
        if not request.is_json:
            abort(415)
        try:
            update = types.Update.de_json(request.get_data(as_text=True))
        except (ValueError, TypeError, KeyError):
            abort(400)
        bot.process_new_updates([update])
        return "", 204

    return app


def run_standalone():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_environment()
    bot = telebot.TeleBot(settings.bot_token, threaded=True)
    relay_settings = RelaySettings.environment(settings.database_url)
    relay = VkRelay(bot, relay_settings) if relay_settings else None
    register_commands(bot, lambda: relay, settings.owner_ids)

    @bot.message_handler(commands=["myid", "id"])
    def myid(message):
        bot.reply_to(message, f"Ваш Telegram ID: {message.from_user.id}\nID этого чата: {message.chat.id}")

    @bot.message_handler(commands=["start", "help"])
    def help_command(message):
        bot.reply_to(message, "Пересылка ВК → Telegram. /vk_test — тест последнего сообщения (для владельца). /myid — ID чата.")

    bot.set_my_commands([types.BotCommand("vk_test", "тест последнего сообщения ВК"),
                         types.BotCommand("myid", "показать ID чата"),
                         types.BotCommand("help", "помощь")])
    if settings.webhook_url:
        bot.set_webhook(url=settings.webhook_url + "/webhook",
                        secret_token=settings.webhook_secret, allowed_updates=["message"])
    else:
        bot.remove_webhook()
        threading.Thread(target=bot.infinity_polling,
                         kwargs={"skip_pending": True, "timeout": 30, "long_polling_timeout": 30},
                         daemon=True).start()
    app = create_web_app(bot, settings, relay)
    if relay:
        relay.start()
    try:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True, use_reloader=False)
    finally:
        if relay:
            relay.shutdown()
        bot.stop_polling()
