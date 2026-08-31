from __future__ import annotations

import logging
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import telebot
from apscheduler.schedulers.background import BackgroundScheduler

from database import Repository
from schedule_service import notification_messages

LOGGER = logging.getLogger(__name__)


class NotificationDispatcher:
    def __init__(self, bot: telebot.TeleBot, repository: Repository):
        self.bot = bot
        self.repository = repository
        self.scheduler = BackgroundScheduler(timezone="UTC")

    def start(self) -> None:
        self.scheduler.add_job(
            self.run_once,
            trigger="interval",
            seconds=30,
            id="group-morning-notifications",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            next_run_time=datetime.now(UTC),
        )
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def run_once(self, now: datetime | None = None) -> None:
        current_utc = now or datetime.now(UTC)
        if current_utc.tzinfo is None:
            current_utc = current_utc.replace(tzinfo=UTC)

        for group, notification in self.repository.list_enabled_notifications():
            local_now = current_utc.astimezone(ZoneInfo(group.timezone))
            hour, minute = map(int, notification.notification_time.split(":"))
            scheduled = datetime.combine(
                local_now.date(), time(hour=hour, minute=minute), local_now.tzinfo
            )
            seconds_after_schedule = (local_now - scheduled).total_seconds()
            if not 0 <= seconds_after_schedule < 600:
                continue
            if not self.repository.claim_notification(group.chat_id, local_now.date()):
                continue

            try:
                for text in notification_messages(
                    self.repository, group, local_now.date()
                ):
                    self.bot.send_message(group.chat_id, text)
            except Exception:
                self.repository.release_notification(group.chat_id, local_now.date())
                LOGGER.exception(
                    "Не удалось отправить утреннее расписание в группу %s",
                    group.chat_id,
                )
