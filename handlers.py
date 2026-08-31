from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import telebot
from telebot import types

from config import Settings
from database import Group, Repository
from schedule_service import (
    DAYS_RU,
    SHORT_DAYS_RU,
    WEEK_LABELS,
    current_week_type,
    format_schedule,
    monday_for,
    target_date_for_action,
)

LOGGER = logging.getLogger(__name__)
GROUP_CHAT_TYPES = {"group", "supergroup"}


class CallbackNotice(Exception):
    def __init__(self, text: str, show_alert: bool = True):
        super().__init__(text)
        self.text = text
        self.show_alert = show_alert


def display_name(user: types.User) -> str:
    full_name = " ".join(part for part in (user.first_name, user.last_name) if part)
    return full_name or (f"@{user.username}" if user.username else str(user.id))


def truncate(value: str, limit: int = 36) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


class BotHandlers:
    def __init__(
        self, bot: telebot.TeleBot, repository: Repository, settings: Settings
    ):
        self.bot = bot
        self.repository = repository
        self.settings = settings
        self.template_path = Path(__file__).with_name("initial_schedule.json")

    def register(self) -> None:
        self.bot.register_message_handler(self.start, commands=["start", "schedule"])
        self.bot.register_message_handler(self.my_id, commands=["myid", "id"])
        self.bot.register_message_handler(self.admin, commands=["admin"])
        self.bot.register_message_handler(self.setup_group, commands=["setup"])
        self.bot.register_message_handler(self.settings_menu, commands=["settings"])
        self.bot.register_message_handler(self.cancel, commands=["cancel"])
        self.bot.register_message_handler(
            self.list_moderators_command, commands=["mods"]
        )
        self.bot.register_message_handler(
            self.add_moderator_command, commands=["mod_add"]
        )
        self.bot.register_message_handler(
            self.remove_moderator_command, commands=["mod_remove"]
        )
        self.bot.register_callback_query_handler(
            self.handle_callback, func=lambda call: True
        )
        self.bot.register_message_handler(
            self.handle_pending_text,
            content_types=["text"],
            func=lambda message: True,
        )

    def is_global_owner(self, user_id: int) -> bool:
        return user_id in self.settings.owner_ids

    def can_manage_group(self, group_id: int, user_id: int) -> bool:
        return self.is_global_owner(user_id) or self.repository.get_role(
            group_id, user_id
        ) in {
            "owner",
            "moderator",
        }

    def can_manage_moderators(self, group_id: int, user_id: int) -> bool:
        return (
            self.is_global_owner(user_id)
            or self.repository.get_role(group_id, user_id) == "owner"
        )

    def is_telegram_admin(self, group_id: int, user_id: int) -> bool:
        try:
            member = self.bot.get_chat_member(group_id, user_id)
            return member.status in {"creator", "administrator"}
        except telebot.apihelper.ApiTelegramException:
            LOGGER.exception("Не удалось проверить права Telegram-администратора")
            return False

    def schedule_keyboard(
        self, group_id: int, user_id: int, show_settings: bool = False
    ) -> types.InlineKeyboardMarkup:
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("Сегодня", callback_data="view:today"),
            types.InlineKeyboardButton("Завтра", callback_data="view:tomorrow"),
        )
        markup.row(
            *[
                types.InlineKeyboardButton(label, callback_data=f"view:day:{index}")
                for index, label in enumerate(SHORT_DAYS_RU[:4])
            ]
        )
        markup.row(
            *[
                types.InlineKeyboardButton(label, callback_data=f"view:day:{index}")
                for index, label in enumerate(SHORT_DAYS_RU[4:], start=4)
            ]
        )
        subgroups = self.repository.list_subgroups(group_id)
        if subgroups:
            selected = self.repository.get_user_subgroup(group_id, user_id)
            label = (
                f"👤 {truncate(selected.name, 28)}"
                if selected is not None
                else "👤 Выбрать подгруппу"
            )
            markup.row(
                types.InlineKeyboardButton(label, callback_data="view:subgroups")
            )
        if show_settings:
            markup.row(
                types.InlineKeyboardButton(
                    "⚙️ Настройки группы", callback_data="cfg:home"
                )
            )
        return markup

    @staticmethod
    def admin_keyboard() -> types.InlineKeyboardMarkup:
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton(
                "➕ Создать код подключения", callback_data="adm:new"
            )
        )
        markup.row(
            types.InlineKeyboardButton(
                "👥 Подключённые группы", callback_data="adm:groups"
            ),
            types.InlineKeyboardButton("🎟 Активные коды", callback_data="adm:codes"),
        )
        return markup

    @staticmethod
    def back_button(callback_data: str) -> types.InlineKeyboardMarkup:
        markup = types.InlineKeyboardMarkup()
        markup.row(types.InlineKeyboardButton("← Назад", callback_data=callback_data))
        return markup

    def start(self, message: types.Message) -> None:
        if message.chat.type == "private":
            if self.is_global_owner(message.from_user.id):
                self.bot.send_message(
                    message.chat.id,
                    "Панель владельца бота. Здесь создаются одноразовые коды для новых групп.",
                    reply_markup=self.admin_keyboard(),
                )
            else:
                self.bot.send_message(
                    message.chat.id,
                    "Добавьте меня в учебную группу. Администратор группы сможет подключить её командой /setup КОД.",
                )
            return

        group = self.repository.get_group(message.chat.id)
        if group is None:
            self.bot.reply_to(
                message,
                "Эта группа ещё не подключена. Администратор группы должен использовать /setup КОД.",
            )
            return

        self.bot.send_message(
            message.chat.id,
            group.welcome_text,
            reply_markup=self.schedule_keyboard(
                message.chat.id,
                message.from_user.id,
                self.can_manage_group(message.chat.id, message.from_user.id),
            ),
        )

    def my_id(self, message: types.Message) -> None:
        self.bot.reply_to(message, f"Ваш Telegram ID: {message.from_user.id}")

    def admin(self, message: types.Message) -> None:
        if message.chat.type != "private" or not self.is_global_owner(
            message.from_user.id
        ):
            self.bot.reply_to(
                message, "Эта команда доступна только владельцу бота в личном чате."
            )
            return
        self.bot.send_message(
            message.chat.id,
            "Панель владельца бота:",
            reply_markup=self.admin_keyboard(),
        )

    def setup_group(self, message: types.Message) -> None:
        if message.chat.type not in GROUP_CHAT_TYPES:
            self.bot.reply_to(
                message, "Команда /setup используется внутри Telegram-группы."
            )
            return
        if not self.is_telegram_admin(message.chat.id, message.from_user.id):
            self.bot.reply_to(
                message,
                "Подключить бота может только администратор этой Telegram-группы.",
            )
            return

        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            self.bot.reply_to(message, "Использование: /setup КОД")
            return

        timezone = ZoneInfo(self.settings.default_timezone)
        today = datetime.now(timezone).date()
        anchor_week = "upper" if today.isocalendar().week % 2 == 1 else "lower"
        result = self.repository.consume_setup_code(
            code=parts[1],
            group_id=message.chat.id,
            group_title=message.chat.title or str(message.chat.id),
            user_id=message.from_user.id,
            display_name=display_name(message.from_user),
            timezone=self.settings.default_timezone,
            anchor_monday=monday_for(today),
            anchor_week_type=anchor_week,
        )
        if not result.ok:
            self.bot.reply_to(message, result.message)
            return

        if result.template_key == "legacy":
            try:
                template = json.loads(self.template_path.read_text(encoding="utf-8"))
                self.repository.replace_schedule_from_template(
                    message.chat.id, template
                )
            except (OSError, ValueError):
                LOGGER.exception("Не удалось импортировать начальное расписание")
                self.bot.reply_to(
                    message,
                    "Группа подключена, но шаблон расписания не импортировался. Его можно заполнить через /settings.",
                )
                return

        self.bot.reply_to(
            message,
            "Группа подключена ✅\n"
            "Вы назначены владельцем настроек этой группы. Откройте /settings, чтобы заполнить расписание и добавить модераторов.",
            reply_markup=self.settings_keyboard(),
        )

    def settings_menu(self, message: types.Message) -> None:
        if message.chat.type not in GROUP_CHAT_TYPES:
            self.bot.reply_to(
                message, "Настройки группы открываются командой /settings внутри неё."
            )
            return
        if not self.can_manage_group(message.chat.id, message.from_user.id):
            self.bot.reply_to(
                message, "У вас нет прав на изменение настроек этой группы."
            )
            return
        self.send_settings_panel(message.chat.id)

    def cancel(self, message: types.Message) -> None:
        self.repository.clear_state(message.chat.id, message.from_user.id)
        self.bot.reply_to(message, "Ввод отменён.")

    @staticmethod
    def settings_keyboard() -> types.InlineKeyboardMarkup:
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton(
                "📚 Редактировать расписание", callback_data="cfg:schedule"
            )
        )
        markup.row(
            types.InlineKeyboardButton(
                "🔁 Чередование недель", callback_data="cfg:anchor"
            ),
            types.InlineKeyboardButton("🌍 Часовой пояс", callback_data="cfg:timezone"),
        )
        markup.row(
            types.InlineKeyboardButton("✏️ Название", callback_data="cfg:title"),
            types.InlineKeyboardButton("💬 Тексты", callback_data="cfg:texts"),
        )
        markup.row(
            types.InlineKeyboardButton(
                "🌅 Утренняя рассылка", callback_data="cfg:notify"
            ),
            types.InlineKeyboardButton("👥 Подгруппы", callback_data="cfg:subs"),
        )
        markup.row(types.InlineKeyboardButton("🛡 Модераторы", callback_data="cfg:mods"))
        markup.row(
            types.InlineKeyboardButton("🗓 Открыть расписание", callback_data="cfg:show")
        )
        return markup

    def settings_text(self, group: Group) -> str:
        week_type = current_week_type(group)
        notification = self.repository.get_notification_settings(group.chat_id)
        notification_text = (
            f"включена, {notification.notification_time}"
            if notification.enabled
            else "выключена"
        )
        return (
            f"⚙️ Настройки «{group.title}»\n\n"
            f"Часовой пояс: {group.timezone}\n"
            f"Текущая неделя: {WEEK_LABELS[week_type]}\n\n"
            f"Утренняя рассылка: {notification_text}\n"
            f"Подгрупп: {len(self.repository.list_subgroups(group.chat_id))}\n\n"
            "Расписание и права относятся только к этой группе."
        )

    def send_settings_panel(self, chat_id: int) -> None:
        group = self.repository.get_group(chat_id)
        if group is None:
            self.bot.send_message(chat_id, "Группа ещё не настроена.")
            return
        self.bot.send_message(
            chat_id, self.settings_text(group), reply_markup=self.settings_keyboard()
        )

    def safe_edit(
        self,
        call: types.CallbackQuery,
        text: str,
        reply_markup: types.InlineKeyboardMarkup | None = None,
    ) -> None:
        try:
            self.bot.edit_message_text(
                text,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=reply_markup,
            )
        except telebot.apihelper.ApiTelegramException as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    def handle_callback(self, call: types.CallbackQuery) -> None:
        try:
            if call.data.startswith("view:"):
                self.handle_view_callback(call)
            elif call.data.startswith("adm:"):
                self.handle_admin_callback(call)
            elif call.data.startswith("cfg:"):
                self.handle_settings_callback(call)
            else:
                raise CallbackNotice("Кнопка устарела.", show_alert=False)
            self.bot.answer_callback_query(call.id)
        except CallbackNotice as notice:
            self.bot.answer_callback_query(
                call.id, notice.text, show_alert=notice.show_alert
            )
        except Exception:
            LOGGER.exception("Ошибка обработки callback %s", call.data)
            try:
                self.bot.answer_callback_query(
                    call.id, "Не удалось выполнить действие.", show_alert=True
                )
            except telebot.apihelper.ApiTelegramException:
                pass

    def handle_view_callback(self, call: types.CallbackQuery) -> None:
        group = self.repository.get_group(call.message.chat.id)
        if group is None:
            raise CallbackNotice("Группа ещё не подключена.")

        raw_action = call.data.removeprefix("view:")
        if raw_action == "subgroups":
            self.show_view_subgroups(call, group)
            return
        if raw_action.startswith("sub:"):
            subgroup_value = raw_action.split(":", maxsplit=1)[1]
            if subgroup_value == "common":
                self.repository.clear_user_subgroup(group.chat_id, call.from_user.id)
            elif not self.repository.set_user_subgroup(
                group.chat_id, call.from_user.id, int(subgroup_value)
            ):
                raise CallbackNotice("Подгруппа уже удалена.")
            raw_action = "today"

        target_date = target_date_for_action(group, raw_action)
        selected = self.repository.get_user_subgroup(group.chat_id, call.from_user.id)
        text = format_schedule(
            self.repository,
            group,
            target_date,
            selected.id if selected is not None else None,
        )
        if selected is None and self.repository.list_subgroups(group.chat_id):
            text += (
                "\n\nℹ️ Сейчас показаны только общие занятия. Выберите свою подгруппу."
            )
        self.safe_edit(
            call,
            text,
            self.schedule_keyboard(
                group.chat_id,
                call.from_user.id,
                self.can_manage_group(group.chat_id, call.from_user.id),
            ),
        )

    def show_view_subgroups(self, call: types.CallbackQuery, group: Group) -> None:
        subgroups = self.repository.list_subgroups(group.chat_id)
        if not subgroups:
            raise CallbackNotice("В этой группе подгруппы не настроены.")
        selected = self.repository.get_user_subgroup(group.chat_id, call.from_user.id)
        markup = types.InlineKeyboardMarkup()
        for subgroup in subgroups:
            prefix = "✅ " if selected and selected.id == subgroup.id else ""
            markup.row(
                types.InlineKeyboardButton(
                    f"{prefix}{truncate(subgroup.name, 32)}",
                    callback_data=f"view:sub:{subgroup.id}",
                )
            )
        markup.row(
            types.InlineKeyboardButton(
                "Только общие занятия", callback_data="view:sub:common"
            )
        )
        markup.row(types.InlineKeyboardButton("← Назад", callback_data="view:today"))
        self.safe_edit(call, "Выберите свою подгруппу:", markup)

    def handle_admin_callback(self, call: types.CallbackQuery) -> None:
        if call.message.chat.type != "private" or not self.is_global_owner(
            call.from_user.id
        ):
            raise CallbackNotice("Нет доступа.")

        action = call.data.split(":")
        if action[1] == "home":
            self.safe_edit(call, "Панель владельца бота:", self.admin_keyboard())
        elif action[1] == "new":
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton(
                    "Пустое расписание", callback_data="adm:code:blank"
                )
            )
            markup.row(
                types.InlineKeyboardButton(
                    "Скопировать старое расписание", callback_data="adm:code:legacy"
                )
            )
            markup.row(types.InlineKeyboardButton("← Назад", callback_data="adm:home"))
            self.safe_edit(
                call,
                "Какое расписание создать в новой группе? Его можно полностью изменить после подключения.",
                markup,
            )
        elif action[1] == "code" and len(action) == 3:
            code = self.repository.create_setup_code(
                created_by=call.from_user.id,
                template_key=action[2],
                ttl_hours=self.settings.setup_code_ttl_hours,
            )
            text = (
                f"Код подключения: {code}\n\n"
                f"Действует {self.settings.setup_code_ttl_hours} ч. и используется один раз.\n"
                "Передайте его администратору группы. После добавления бота он должен выполнить в группе:\n\n"
                f"/setup {code}"
            )
            self.safe_edit(call, text, self.back_button("adm:home"))
        elif action[1] == "groups":
            groups = self.repository.list_groups()
            lines = ["Подключённые группы:"]
            lines.extend(
                f"• {truncate(group.title, 60)} ({group.chat_id})"
                for group in groups[:40]
            )
            if not groups:
                lines.append("Пока ни одной.")
            self.safe_edit(call, "\n".join(lines), self.back_button("adm:home"))
        elif action[1] == "codes":
            codes = self.repository.list_active_codes(call.from_user.id)
            lines = ["Активные одноразовые коды:"]
            for code in codes:
                template = (
                    "старое расписание" if code.template_key == "legacy" else "пустое"
                )
                lines.append(
                    f"• {code.code_hint} — {template}, до {code.expires_at:%d.%m %H:%M} UTC"
                )
            if not codes:
                lines.append("Активных кодов нет.")
            self.safe_edit(call, "\n".join(lines), self.back_button("adm:home"))

    def handle_settings_callback(self, call: types.CallbackQuery) -> None:
        group_id = call.message.chat.id
        if call.message.chat.type not in GROUP_CHAT_TYPES:
            raise CallbackNotice("Настройки доступны только в группе.")
        if not self.can_manage_group(group_id, call.from_user.id):
            raise CallbackNotice("Нет прав на изменение этой группы.")

        parts = call.data.split(":")
        action = parts[1]
        group = self.repository.get_group(group_id)
        if group is None:
            raise CallbackNotice("Группа не настроена.")

        if action == "home":
            self.safe_edit(call, self.settings_text(group), self.settings_keyboard())
        elif action == "show":
            self.safe_edit(
                call,
                group.welcome_text,
                self.schedule_keyboard(
                    group_id,
                    call.from_user.id,
                    self.can_manage_group(group_id, call.from_user.id),
                ),
            )
        elif action == "schedule":
            self.show_schedule_scopes(call)
        elif action == "scope" and len(parts) == 3:
            self.show_scope_weeks(call, parts[2])
        elif action == "week" and len(parts) == 4:
            self.show_week_editor(call, parts[2], parts[3])
        elif action == "day" and len(parts) == 5:
            self.show_day_editor(call, parts[2], parts[3], int(parts[4]))
        elif action == "add" and len(parts) == 5:
            subgroup_id = self.subgroup_id_from_token(group_id, parts[2])
            self.begin_text_input(
                call,
                "add_entry",
                {
                    "subgroup_id": subgroup_id,
                    "week_type": parts[3],
                    "day_of_week": int(parts[4]),
                },
                "Ответьте на это сообщение текстом занятия. Формат свободный, например:\n"
                "1. Математика, ауд. 204, 08:00–09:30\n\nОтмена: /cancel",
            )
        elif action == "edit" and len(parts) == 3:
            entry = self.repository.get_schedule_entry(group_id, int(parts[2]))
            if entry is None:
                raise CallbackNotice("Занятие уже удалено.")
            self.begin_text_input(
                call,
                "edit_entry",
                {"entry_id": entry.id},
                f"Ответьте на это сообщение новым текстом занятия:\n\n{entry.text}\n\nОтмена: /cancel",
            )
        elif action == "delask" and len(parts) == 3:
            entry = self.repository.get_schedule_entry(group_id, int(parts[2]))
            if entry is None:
                raise CallbackNotice("Занятие уже удалено.")
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton(
                    "Удалить", callback_data=f"cfg:delete:{entry.id}"
                ),
                types.InlineKeyboardButton(
                    "Отмена",
                    callback_data=(
                        f"cfg:day:{self.scope_token(entry.subgroup_id)}:"
                        f"{entry.week_type}:{entry.day_of_week}"
                    ),
                ),
            )
            self.safe_edit(call, f"Удалить это занятие?\n\n{entry.text}", markup)
        elif action == "delete" and len(parts) == 3:
            entry = self.repository.get_schedule_entry(group_id, int(parts[2]))
            if entry is None:
                raise CallbackNotice("Занятие уже удалено.")
            week_type, day_of_week = entry.week_type, entry.day_of_week
            subgroup_id = entry.subgroup_id
            self.repository.delete_schedule_entry(group_id, entry.id)
            self.show_day_editor(
                call, self.scope_token(subgroup_id), week_type, day_of_week
            )
        elif action == "move" and len(parts) == 4:
            entry = self.repository.move_schedule_entry(
                group_id, int(parts[2]), parts[3]
            )
            if entry is None:
                raise CallbackNotice("Занятие уже удалено.")
            self.show_day_editor(
                call,
                self.scope_token(entry.subgroup_id),
                entry.week_type,
                entry.day_of_week,
            )
        elif action == "anchor":
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton(
                    "Сейчас верхняя", callback_data="cfg:setweek:upper"
                ),
                types.InlineKeyboardButton(
                    "Сейчас нижняя", callback_data="cfg:setweek:lower"
                ),
            )
            markup.row(
                types.InlineKeyboardButton("← Настройки", callback_data="cfg:home")
            )
            self.safe_edit(
                call,
                "Укажите тип текущей недели. Дальше бот будет автоматически чередовать недели каждый понедельник.",
                markup,
            )
        elif action == "setweek" and len(parts) == 3:
            if parts[2] not in WEEK_LABELS:
                raise CallbackNotice("Неизвестный тип недели.")
            today = datetime.now(ZoneInfo(group.timezone)).date()
            group = (
                self.repository.update_group(
                    group_id,
                    anchor_monday=monday_for(today),
                    anchor_week_type=parts[2],
                )
                or group
            )
            self.safe_edit(call, self.settings_text(group), self.settings_keyboard())
        elif action == "timezone":
            self.begin_text_input(
                call,
                "timezone",
                {},
                "Ответьте на это сообщение часовым поясом в формате Europe/Moscow, Europe/Kaliningrad и т. п.\n\nОтмена: /cancel",
            )
        elif action == "title":
            self.begin_text_input(
                call,
                "title",
                {},
                "Ответьте на это сообщение названием расписания (до 100 символов).\n\nОтмена: /cancel",
            )
        elif action == "texts":
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("Приветствие", callback_data="cfg:welcome")
            )
            markup.row(
                types.InlineKeyboardButton(
                    "Текст пустого дня", callback_data="cfg:empty"
                )
            )
            markup.row(
                types.InlineKeyboardButton("← Настройки", callback_data="cfg:home")
            )
            self.safe_edit(
                call,
                f"Тексты группы:\n\nПриветствие:\n{group.welcome_text}\n\nЕсли занятий нет:\n{group.empty_day_text}",
                markup,
            )
        elif action == "welcome":
            self.begin_text_input(
                call,
                "welcome_text",
                {},
                "Ответьте на это сообщение новым приветствием (до 500 символов).\n\nОтмена: /cancel",
            )
        elif action == "empty":
            self.begin_text_input(
                call,
                "empty_day_text",
                {},
                "Ответьте на это сообщение текстом для дня без занятий (до 500 символов).\n\nОтмена: /cancel",
            )
        elif action == "notify":
            self.show_notification_settings(call)
        elif action == "notoggle":
            notification = self.repository.get_notification_settings(group_id)
            self.repository.update_notification_settings(
                group_id, enabled=not notification.enabled
            )
            self.show_notification_settings(call)
        elif action == "nottime":
            self.begin_text_input(
                call,
                "notification_time",
                {},
                "Ответьте на это сообщение временем утренней рассылки в формате ЧЧ:ММ, например 07:30. Используется часовой пояс группы.\n\nОтмена: /cancel",
            )
        elif action == "subs":
            self.show_subgroups_settings(call)
        elif action == "subadd":
            self.begin_text_input(
                call,
                "subgroup_add",
                {},
                "Ответьте на это сообщение названием новой подгруппы (до 40 символов).\n\nОтмена: /cancel",
            )
        elif action == "subedit" and len(parts) == 3:
            subgroup = self.repository.get_subgroup(group_id, int(parts[2]))
            if subgroup is None:
                raise CallbackNotice("Подгруппа уже удалена.")
            self.begin_text_input(
                call,
                "subgroup_rename",
                {"subgroup_id": subgroup.id},
                f"Ответьте новым названием для «{subgroup.name}» (до 40 символов).\n\nОтмена: /cancel",
            )
        elif action == "subdelask" and len(parts) == 3:
            self.confirm_delete_subgroup(call, int(parts[2]))
        elif action == "subdel" and len(parts) == 3:
            self.repository.delete_subgroup(group_id, int(parts[2]))
            self.show_subgroups_settings(call)
        elif action == "mods":
            self.show_moderators(call)
        elif action == "modpick":
            self.show_admin_candidates(call)
        elif action == "modadd" and len(parts) == 3:
            self.add_moderator_from_callback(call, int(parts[2]))
        elif action == "modrmask" and len(parts) == 3:
            self.confirm_remove_moderator(call, int(parts[2]))
        elif action == "modrm" and len(parts) == 3:
            if not self.can_manage_moderators(group_id, call.from_user.id):
                raise CallbackNotice("Только владелец группы может менять роли.")
            self.repository.remove_moderator(group_id, int(parts[2]))
            self.show_moderators(call)
        elif action == "ownerpick":
            self.show_owner_candidates(call)
        elif action == "ownerask" and len(parts) == 3:
            self.confirm_transfer_owner(call, int(parts[2]))
        elif action == "owner" and len(parts) == 3:
            self.transfer_owner_from_callback(call, int(parts[2]))

    @staticmethod
    def scope_token(subgroup_id: int | None) -> str:
        return "common" if subgroup_id is None else str(subgroup_id)

    def subgroup_id_from_token(self, group_id: int, scope_token: str) -> int | None:
        if scope_token == "common":
            return None
        subgroup_id = int(scope_token)
        if self.repository.get_subgroup(group_id, subgroup_id) is None:
            raise CallbackNotice("Подгруппа уже удалена.")
        return subgroup_id

    def scope_label(self, group_id: int, scope_token: str) -> str:
        subgroup_id = self.subgroup_id_from_token(group_id, scope_token)
        if subgroup_id is None:
            return "Общее для всех"
        subgroup = self.repository.get_subgroup(group_id, subgroup_id)
        return subgroup.name if subgroup is not None else "Подгруппа"

    def show_schedule_scopes(self, call: types.CallbackQuery) -> None:
        group_id = call.message.chat.id
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton(
                "Общее для всех", callback_data="cfg:scope:common"
            )
        )
        for subgroup in self.repository.list_subgroups(group_id):
            markup.row(
                types.InlineKeyboardButton(
                    f"👤 {truncate(subgroup.name, 32)}",
                    callback_data=f"cfg:scope:{subgroup.id}",
                )
            )
        markup.row(types.InlineKeyboardButton("← Настройки", callback_data="cfg:home"))
        self.safe_edit(
            call,
            "Какое расписание редактировать? Общие занятия показываются всем, занятия подгруппы — только выбравшим её участникам.",
            markup,
        )

    def show_scope_weeks(self, call: types.CallbackQuery, scope_token: str) -> None:
        group_id = call.message.chat.id
        label = self.scope_label(group_id, scope_token)
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton(
                "Верхняя", callback_data=f"cfg:week:{scope_token}:upper"
            ),
            types.InlineKeyboardButton(
                "Нижняя", callback_data=f"cfg:week:{scope_token}:lower"
            ),
        )
        markup.row(
            types.InlineKeyboardButton("← Расписания", callback_data="cfg:schedule")
        )
        self.safe_edit(call, f"{label}. Выберите неделю:", markup)

    def show_week_editor(
        self, call: types.CallbackQuery, scope_token: str, week_type: str
    ) -> None:
        if week_type not in WEEK_LABELS:
            raise ValueError("Неизвестный тип недели")
        self.subgroup_id_from_token(call.message.chat.id, scope_token)
        markup = types.InlineKeyboardMarkup()
        for start in (0, 3, 6):
            buttons = [
                types.InlineKeyboardButton(
                    SHORT_DAYS_RU[index],
                    callback_data=f"cfg:day:{scope_token}:{week_type}:{index}",
                )
                for index in range(start, min(start + 3, 7))
            ]
            markup.row(*buttons)
        markup.row(
            types.InlineKeyboardButton(
                "← Выбор недели", callback_data=f"cfg:scope:{scope_token}"
            )
        )
        self.safe_edit(call, f"{WEEK_LABELS[week_type]} неделя. Выберите день:", markup)

    def day_editor_content(
        self,
        group_id: int,
        scope_token: str,
        week_type: str,
        day_of_week: int,
    ) -> tuple[str, types.InlineKeyboardMarkup]:
        if week_type not in WEEK_LABELS or day_of_week not in range(7):
            raise ValueError("Некорректная неделя или день")
        subgroup_id = self.subgroup_id_from_token(group_id, scope_token)
        entries = self.repository.list_schedule(
            group_id, week_type, day_of_week, subgroup_id=subgroup_id
        )
        lines = [
            (
                f"{self.scope_label(group_id, scope_token)}\n"
                f"{DAYS_RU[day_of_week]}, {WEEK_LABELS[week_type].lower()} неделя:"
            )
        ]
        lines.extend(
            f"{index}. {entry.text}" for index, entry in enumerate(entries, start=1)
        )
        if not entries:
            lines.append("Занятий пока нет.")

        markup = types.InlineKeyboardMarkup()
        for index, entry in enumerate(entries, start=1):
            markup.row(
                types.InlineKeyboardButton(
                    f"✏️ {index}. {truncate(entry.text)}",
                    callback_data=f"cfg:edit:{entry.id}",
                ),
                types.InlineKeyboardButton(
                    "↑", callback_data=f"cfg:move:{entry.id}:up"
                ),
                types.InlineKeyboardButton(
                    "↓", callback_data=f"cfg:move:{entry.id}:down"
                ),
                types.InlineKeyboardButton("🗑", callback_data=f"cfg:delask:{entry.id}"),
            )
        markup.row(
            types.InlineKeyboardButton(
                "➕ Добавить занятие",
                callback_data=f"cfg:add:{scope_token}:{week_type}:{day_of_week}",
            )
        )
        markup.row(
            types.InlineKeyboardButton(
                "← Дни недели", callback_data=f"cfg:week:{scope_token}:{week_type}"
            )
        )
        return "\n\n".join(lines), markup

    def show_day_editor(
        self,
        call: types.CallbackQuery,
        scope_token: str,
        week_type: str,
        day_of_week: int,
    ) -> None:
        text, markup = self.day_editor_content(
            call.message.chat.id, scope_token, week_type, day_of_week
        )
        self.safe_edit(call, text, markup)

    def begin_text_input(
        self,
        call: types.CallbackQuery,
        action: str,
        payload: dict,
        prompt: str,
    ) -> None:
        sent = self.bot.send_message(
            call.message.chat.id,
            prompt,
            reply_markup=types.ForceReply(selective=True),
        )
        self.repository.set_state(
            group_id=call.message.chat.id,
            user_id=call.from_user.id,
            action=action,
            payload=payload,
            prompt_message_id=sent.message_id,
        )

    def handle_pending_text(self, message: types.Message) -> None:
        state = self.repository.get_state(message.chat.id, message.from_user.id)
        if state is None:
            return
        if not self.can_manage_group(message.chat.id, message.from_user.id):
            self.repository.clear_state(message.chat.id, message.from_user.id)
            return
        if state.prompt_message_id is not None:
            reply = message.reply_to_message
            if reply is None or reply.message_id != state.prompt_message_id:
                return

        value = (message.text or "").strip()
        if not value:
            self.bot.reply_to(
                message,
                "Значение не может быть пустым. Попробуйте ещё раз или /cancel.",
            )
            return

        try:
            if state.action == "add_entry":
                if len(value) > 300:
                    raise ValueError(
                        "Текст занятия должен быть не длиннее 300 символов."
                    )
                current_entries = self.repository.list_schedule(
                    message.chat.id,
                    state.payload["week_type"],
                    int(state.payload["day_of_week"]),
                    subgroup_id=state.payload.get("subgroup_id"),
                )
                if len(current_entries) >= 12:
                    raise ValueError(
                        "На один день можно добавить не больше 12 занятий."
                    )
                self.repository.add_schedule_entry(
                    message.chat.id,
                    state.payload["week_type"],
                    int(state.payload["day_of_week"]),
                    value,
                    subgroup_id=state.payload.get("subgroup_id"),
                )
                result_text = "Занятие добавлено ✅"
            elif state.action == "edit_entry":
                if len(value) > 300:
                    raise ValueError(
                        "Текст занятия должен быть не длиннее 300 символов."
                    )
                if not self.repository.update_schedule_entry(
                    message.chat.id, int(state.payload["entry_id"]), value
                ):
                    raise ValueError("Занятие уже удалено.")
                result_text = "Занятие обновлено ✅"
            elif state.action == "timezone":
                try:
                    ZoneInfo(value)
                except ZoneInfoNotFoundError as exc:
                    raise ValueError(
                        "Неизвестный часовой пояс. Пример: Europe/Moscow"
                    ) from exc
                self.repository.update_group(message.chat.id, timezone=value)
                result_text = "Часовой пояс обновлён ✅"
            elif state.action == "title":
                if len(value) > 100:
                    raise ValueError("Название должно быть не длиннее 100 символов.")
                self.repository.update_group(message.chat.id, title=value)
                result_text = "Название обновлено ✅"
            elif state.action in {"welcome_text", "empty_day_text"}:
                if len(value) > 500:
                    raise ValueError("Текст должен быть не длиннее 500 символов.")
                self.repository.update_group(message.chat.id, **{state.action: value})
                result_text = "Текст обновлён ✅"
            elif state.action == "notification_time":
                if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                    raise ValueError(
                        "Время должно быть в формате ЧЧ:ММ, например 07:30."
                    )
                self.repository.update_notification_settings(
                    message.chat.id, notification_time=value
                )
                result_text = "Время утренней рассылки обновлено ✅"
            elif state.action == "subgroup_add":
                if len(value) > 40:
                    raise ValueError("Название должно быть не длиннее 40 символов.")
                subgroups = self.repository.list_subgroups(message.chat.id)
                if len(subgroups) >= 8:
                    raise ValueError(
                        "В одной группе можно создать не больше 8 подгрупп."
                    )
                if any(item.name.casefold() == value.casefold() for item in subgroups):
                    raise ValueError("Подгруппа с таким названием уже существует.")
                self.repository.add_subgroup(message.chat.id, value)
                result_text = "Подгруппа добавлена ✅"
            elif state.action == "subgroup_rename":
                if len(value) > 40:
                    raise ValueError("Название должно быть не длиннее 40 символов.")
                subgroup_id = int(state.payload["subgroup_id"])
                subgroups = self.repository.list_subgroups(message.chat.id)
                if any(
                    item.id != subgroup_id and item.name.casefold() == value.casefold()
                    for item in subgroups
                ):
                    raise ValueError("Подгруппа с таким названием уже существует.")
                if not self.repository.rename_subgroup(
                    message.chat.id, subgroup_id, value
                ):
                    raise ValueError("Подгруппа уже удалена.")
                result_text = "Подгруппа переименована ✅"
            else:
                raise ValueError("Это действие уже не поддерживается.")
        except ValueError as exc:
            self.bot.reply_to(
                message, f"Ошибка: {exc}\nПопробуйте ещё раз или /cancel."
            )
            return

        self.repository.clear_state(message.chat.id, message.from_user.id)
        self.bot.reply_to(message, result_text, reply_markup=self.settings_keyboard())

    def show_notification_settings(self, call: types.CallbackQuery) -> None:
        group = self.repository.get_group(call.message.chat.id)
        if group is None:
            raise CallbackNotice("Группа не настроена.")
        notification = self.repository.get_notification_settings(group.chat_id)
        status = "включена ✅" if notification.enabled else "выключена"
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton(
                "Выключить" if notification.enabled else "Включить",
                callback_data="cfg:notoggle",
            ),
            types.InlineKeyboardButton("Изменить время", callback_data="cfg:nottime"),
        )
        markup.row(types.InlineKeyboardButton("← Настройки", callback_data="cfg:home"))
        self.safe_edit(
            call,
            f"🌅 Утренняя рассылка {status}.\n\n"
            f"Время: {notification.notification_time}\n"
            f"Часовой пояс: {group.timezone}\n\n"
            "Если настроены подгруппы, бот отправит отдельное сообщение для каждой подгруппы.",
            markup,
        )

    def show_subgroups_settings(self, call: types.CallbackQuery) -> None:
        group_id = call.message.chat.id
        subgroups = self.repository.list_subgroups(group_id)
        lines = ["👥 Подгруппы этой группы:"]
        lines.extend(f"• {subgroup.name}" for subgroup in subgroups)
        if not subgroups:
            lines.append("Подгрупп пока нет. Расписание считается общим.")
        lines.append(
            "\nОбщие занятия показываются всем. Для каждой подгруппы можно добавить собственные занятия."
        )

        markup = types.InlineKeyboardMarkup()
        for subgroup in subgroups:
            markup.row(
                types.InlineKeyboardButton(
                    f"✏️ {truncate(subgroup.name, 26)}",
                    callback_data=f"cfg:subedit:{subgroup.id}",
                ),
                types.InlineKeyboardButton(
                    "🗑", callback_data=f"cfg:subdelask:{subgroup.id}"
                ),
            )
        if len(subgroups) < 8:
            markup.row(
                types.InlineKeyboardButton(
                    "➕ Добавить подгруппу", callback_data="cfg:subadd"
                )
            )
        markup.row(types.InlineKeyboardButton("← Настройки", callback_data="cfg:home"))
        self.safe_edit(call, "\n".join(lines), markup)

    def confirm_delete_subgroup(
        self, call: types.CallbackQuery, subgroup_id: int
    ) -> None:
        subgroup = self.repository.get_subgroup(call.message.chat.id, subgroup_id)
        if subgroup is None:
            raise CallbackNotice("Подгруппа уже удалена.")
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton(
                "Удалить вместе с расписанием",
                callback_data=f"cfg:subdel:{subgroup.id}",
            ),
            types.InlineKeyboardButton("Отмена", callback_data="cfg:subs"),
        )
        self.safe_edit(
            call,
            f"Удалить подгруппу «{subgroup.name}»? Её отдельное расписание и выбор пользователей будут удалены.",
            markup,
        )

    def moderators_text(self, group_id: int) -> str:
        moderators = self.repository.list_moderators(group_id)
        lines = ["🛡 Редакторы этой группы:"]
        for moderator in moderators:
            role = "владелец" if moderator.role == "owner" else "модератор"
            lines.append(f"• {moderator.display_name} — {role} ({moderator.user_id})")
        lines.append(
            "\nДобавлять можно Telegram-администраторов кнопкой ниже или любого участника командой /mod_add в ответ на его сообщение."
        )
        return "\n".join(lines)

    def moderators_keyboard(
        self, group_id: int, viewer_id: int
    ) -> types.InlineKeyboardMarkup:
        markup = types.InlineKeyboardMarkup()
        if self.can_manage_moderators(group_id, viewer_id):
            markup.row(
                types.InlineKeyboardButton(
                    "➕ Добавить администратора", callback_data="cfg:modpick"
                )
            )
            for moderator in self.repository.list_moderators(group_id):
                if moderator.role != "owner":
                    markup.row(
                        types.InlineKeyboardButton(
                            f"🗑 {truncate(moderator.display_name, 28)}",
                            callback_data=f"cfg:modrmask:{moderator.user_id}",
                        )
                    )
            markup.row(
                types.InlineKeyboardButton(
                    "👑 Передать владение", callback_data="cfg:ownerpick"
                )
            )
        markup.row(types.InlineKeyboardButton("← Настройки", callback_data="cfg:home"))
        return markup

    def show_moderators(self, call: types.CallbackQuery) -> None:
        group_id = call.message.chat.id
        self.safe_edit(
            call,
            self.moderators_text(group_id),
            self.moderators_keyboard(group_id, call.from_user.id),
        )

    def show_admin_candidates(self, call: types.CallbackQuery) -> None:
        group_id = call.message.chat.id
        if not self.can_manage_moderators(group_id, call.from_user.id):
            raise CallbackNotice("Только владелец группы может менять роли.")
        existing_ids = {
            moderator.user_id for moderator in self.repository.list_moderators(group_id)
        }
        candidates = [
            member.user
            for member in self.bot.get_chat_administrators(group_id)
            if not member.user.is_bot and member.user.id not in existing_ids
        ]
        markup = types.InlineKeyboardMarkup()
        for user in candidates[:30]:
            markup.row(
                types.InlineKeyboardButton(
                    f"➕ {truncate(display_name(user), 30)}",
                    callback_data=f"cfg:modadd:{user.id}",
                )
            )
        markup.row(types.InlineKeyboardButton("← Модераторы", callback_data="cfg:mods"))
        text = (
            "Выберите Telegram-администратора:"
            if candidates
            else "Все Telegram-администраторы уже добавлены."
        )
        self.safe_edit(call, text, markup)

    def add_moderator_from_callback(
        self, call: types.CallbackQuery, user_id: int
    ) -> None:
        group_id = call.message.chat.id
        if not self.can_manage_moderators(group_id, call.from_user.id):
            raise CallbackNotice("Только владелец группы может менять роли.")
        member = self.bot.get_chat_member(group_id, user_id)
        if member.status not in {"creator", "administrator"} or member.user.is_bot:
            raise CallbackNotice("Пользователь уже не администратор.")
        self.repository.add_moderator(
            group_id, user_id, display_name(member.user), call.from_user.id
        )
        self.show_moderators(call)

    def confirm_remove_moderator(self, call: types.CallbackQuery, user_id: int) -> None:
        group_id = call.message.chat.id
        if not self.can_manage_moderators(group_id, call.from_user.id):
            raise CallbackNotice("Только владелец группы может менять роли.")
        moderator = next(
            (
                item
                for item in self.repository.list_moderators(group_id)
                if item.user_id == user_id
            ),
            None,
        )
        if moderator is None or moderator.role == "owner":
            raise CallbackNotice("Эту роль нельзя удалить.")
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton(
                "Удалить роль", callback_data=f"cfg:modrm:{user_id}"
            ),
            types.InlineKeyboardButton("Отмена", callback_data="cfg:mods"),
        )
        self.safe_edit(
            call, f"Снять роль модератора с {moderator.display_name}?", markup
        )

    def show_owner_candidates(self, call: types.CallbackQuery) -> None:
        group_id = call.message.chat.id
        if not self.can_manage_moderators(group_id, call.from_user.id):
            raise CallbackNotice("Только владелец группы может передать владение.")

        current_owner = next(
            (
                item
                for item in self.repository.list_moderators(group_id)
                if item.role == "owner"
            ),
            None,
        )
        candidates: dict[int, str] = {
            item.user_id: item.display_name
            for item in self.repository.list_moderators(group_id)
            if item.role != "owner"
        }
        for member in self.bot.get_chat_administrators(group_id):
            if member.user.is_bot:
                continue
            if current_owner and member.user.id == current_owner.user_id:
                continue
            candidates[member.user.id] = display_name(member.user)

        markup = types.InlineKeyboardMarkup()
        for user_id, name in list(candidates.items())[:30]:
            markup.row(
                types.InlineKeyboardButton(
                    f"👑 {truncate(name, 30)}",
                    callback_data=f"cfg:ownerask:{user_id}",
                )
            )
        markup.row(types.InlineKeyboardButton("← Модераторы", callback_data="cfg:mods"))
        text = (
            "Кому передать владение настройками группы? Текущий владелец станет модератором."
            if candidates
            else "Сначала добавьте будущего владельца как модератора."
        )
        self.safe_edit(call, text, markup)

    def confirm_transfer_owner(
        self, call: types.CallbackQuery, target_user_id: int
    ) -> None:
        group_id = call.message.chat.id
        if not self.can_manage_moderators(group_id, call.from_user.id):
            raise CallbackNotice("Только владелец группы может передать владение.")
        member = self.bot.get_chat_member(group_id, target_user_id)
        if member.user.is_bot or member.status in {"left", "kicked"}:
            raise CallbackNotice("Пользователь больше не состоит в группе.")
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton(
                "Передать владение",
                callback_data=f"cfg:owner:{target_user_id}",
            ),
            types.InlineKeyboardButton("Отмена", callback_data="cfg:mods"),
        )
        self.safe_edit(
            call,
            f"Передать владение пользователю {display_name(member.user)}? Вы останетесь модератором.",
            markup,
        )

    def transfer_owner_from_callback(
        self, call: types.CallbackQuery, target_user_id: int
    ) -> None:
        group_id = call.message.chat.id
        if not self.can_manage_moderators(group_id, call.from_user.id):
            raise CallbackNotice("Только владелец группы может передать владение.")
        member = self.bot.get_chat_member(group_id, target_user_id)
        if member.user.is_bot or member.status in {"left", "kicked"}:
            raise CallbackNotice("Пользователь больше не состоит в группе.")
        if not self.repository.transfer_ownership(
            group_id,
            target_user_id,
            display_name(member.user),
            call.from_user.id,
        ):
            raise CallbackNotice("Не удалось передать владение.")
        self.safe_edit(
            call,
            f"Владение передано пользователю {display_name(member.user)} ✅",
            self.back_button("cfg:mods"),
        )

    def list_moderators_command(self, message: types.Message) -> None:
        if message.chat.type not in GROUP_CHAT_TYPES or not self.can_manage_group(
            message.chat.id, message.from_user.id
        ):
            self.bot.reply_to(message, "Нет доступа.")
            return
        self.bot.reply_to(message, self.moderators_text(message.chat.id))

    def add_moderator_command(self, message: types.Message) -> None:
        if message.chat.type not in GROUP_CHAT_TYPES or not self.can_manage_moderators(
            message.chat.id, message.from_user.id
        ):
            self.bot.reply_to(
                message, "Только владелец настроек группы может добавлять модераторов."
            )
            return
        if (
            message.reply_to_message is None
            or message.reply_to_message.from_user.is_bot
        ):
            self.bot.reply_to(
                message, "Ответьте командой /mod_add на сообщение нужного участника."
            )
            return
        target = message.reply_to_message.from_user
        self.repository.add_moderator(
            message.chat.id, target.id, display_name(target), message.from_user.id
        )
        self.bot.reply_to(
            message,
            f"{display_name(target)} теперь может редактировать расписание этой группы.",
        )

    def remove_moderator_command(self, message: types.Message) -> None:
        if message.chat.type not in GROUP_CHAT_TYPES or not self.can_manage_moderators(
            message.chat.id, message.from_user.id
        ):
            self.bot.reply_to(
                message, "Только владелец настроек группы может снимать роли."
            )
            return
        target_id: int | None = None
        if message.reply_to_message is not None:
            target_id = message.reply_to_message.from_user.id
        else:
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                target_id = int(parts[1])
        if target_id is None:
            self.bot.reply_to(
                message,
                "Ответьте /mod_remove на сообщение модератора или укажите его Telegram ID.",
            )
            return
        if self.repository.remove_moderator(message.chat.id, target_id):
            self.bot.reply_to(message, "Роль модератора удалена.")
        else:
            self.bot.reply_to(message, "Модератор не найден или это владелец группы.")
