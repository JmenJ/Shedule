from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utc_now() -> datetime:
    # Naive UTC keeps SQLite and PostgreSQL comparisons consistent.
    return datetime.now(UTC).replace(tzinfo=None)


LEADING_LESSON_NUMBER = re.compile(r"^\s*(?:\d+\.\s+)+")


def normalize_schedule_text(text: str) -> str:
    """Store lesson text without display numbering managed by the bot."""
    return LEADING_LESSON_NUMBER.sub("", text).strip()


class Base(DeclarativeBase):
    pass


class Group(Base):
    __tablename__ = "groups"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    timezone: Mapped[str] = mapped_column(String(64))
    anchor_monday: Mapped[date] = mapped_column(Date)
    anchor_week_type: Mapped[str] = mapped_column(String(5), default="upper")
    welcome_text: Mapped[str] = mapped_column(
        Text,
        default="Привет! Я бот с расписанием. Выбери день на клавиатуре ниже 👇",
    )
    empty_day_text: Mapped[str] = mapped_column(Text, default="Пар нет! Отдыхаем 🥳")
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class GroupModerator(Base):
    __tablename__ = "group_moderators"

    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.chat_id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="moderator")
    display_name: Mapped[str] = mapped_column(String(255))
    added_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Subgroup(Base):
    __tablename__ = "subgroups"
    __table_args__ = (UniqueConstraint("group_id", "name", name="uq_subgroup_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.chat_id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    position: Mapped[int] = mapped_column(Integer)


class ScheduleEntry(Base):
    __tablename__ = "schedule_entries"
    __table_args__ = (
        UniqueConstraint(
            "group_id",
            "subgroup_id",
            "week_type",
            "day_of_week",
            "position",
            name="uq_schedule_position",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.chat_id", ondelete="CASCADE"), index=True
    )
    subgroup_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("subgroups.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    week_type: Mapped[str] = mapped_column(String(5), index=True)
    day_of_week: Mapped[int] = mapped_column(Integer, index=True)
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)


class UserSubgroupPreference(Base):
    __tablename__ = "user_subgroup_preferences"

    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.chat_id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subgroup_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("subgroups.id", ondelete="CASCADE")
    )


class GroupNotification(Base):
    __tablename__ = "group_notifications"

    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.chat_id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    notification_time: Mapped[str] = mapped_column(String(5), default="07:30")


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    local_date: Mapped[date] = mapped_column(Date, primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class SetupCode(Base):
    __tablename__ = "setup_codes"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    code_hint: Mapped[str] = mapped_column(String(8))
    created_by: Mapped[int] = mapped_column(BigInteger)
    template_key: Mapped[str] = mapped_column(String(32), default="blank")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    used_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    used_group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class ConversationState(Base):
    __tablename__ = "conversation_states"

    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    action: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    prompt_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_now, onupdate=utc_now
    )


@dataclass(frozen=True)
class StateData:
    action: str
    payload: dict[str, Any]
    prompt_message_id: int | None


@dataclass(frozen=True)
class CodeUseResult:
    ok: bool
    message: str
    template_key: str | None = None


class Repository:
    CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    def __init__(self, database_url: str):
        connect_args = (
            {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        )
        self.engine = create_engine(
            database_url,
            pool_pre_ping=not database_url.startswith("sqlite"),
            connect_args=connect_args,
        )
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        self.normalize_existing_schedule_entries()

    def normalize_existing_schedule_entries(self) -> int:
        changed = 0
        with self.Session.begin() as session:
            for entry in session.scalars(select(ScheduleEntry)):
                normalized = normalize_schedule_text(entry.text)
                if normalized != entry.text:
                    entry.text = normalized
                    changed += 1
        return changed

    @staticmethod
    def _hash_code(code: str) -> str:
        return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()

    def create_setup_code(
        self, created_by: int, template_key: str, ttl_hours: int
    ) -> str:
        if template_key not in {"blank", "legacy"}:
            raise ValueError("Неизвестный шаблон")

        for _ in range(10):
            code = "".join(secrets.choice(self.CODE_ALPHABET) for _ in range(10))
            code_hash = self._hash_code(code)
            with self.Session.begin() as session:
                if session.get(SetupCode, code_hash) is not None:
                    continue
                session.add(
                    SetupCode(
                        code_hash=code_hash,
                        code_hint=f"••••{code[-4:]}",
                        created_by=created_by,
                        template_key=template_key,
                        expires_at=utc_now() + timedelta(hours=ttl_hours),
                    )
                )
            return code
        raise RuntimeError("Не удалось сгенерировать уникальный код")

    def list_active_codes(self, created_by: int) -> list[SetupCode]:
        with self.Session() as session:
            return list(
                session.scalars(
                    select(SetupCode)
                    .where(
                        SetupCode.created_by == created_by,
                        SetupCode.used_at.is_(None),
                        SetupCode.expires_at > utc_now(),
                    )
                    .order_by(SetupCode.expires_at)
                )
            )

    def consume_setup_code(
        self,
        code: str,
        group_id: int,
        group_title: str,
        user_id: int,
        display_name: str,
        timezone: str,
        anchor_monday: date,
        anchor_week_type: str,
    ) -> CodeUseResult:
        code_hash = self._hash_code(code)
        with self.Session.begin() as session:
            setup_code = session.scalar(
                select(SetupCode)
                .where(SetupCode.code_hash == code_hash)
                .with_for_update()
            )
            if setup_code is None:
                return CodeUseResult(False, "Код не найден.")
            if setup_code.used_at is not None:
                return CodeUseResult(False, "Этот код уже использован.")
            if setup_code.expires_at <= utc_now():
                return CodeUseResult(False, "Срок действия кода истёк.")
            if session.get(Group, group_id) is not None:
                return CodeUseResult(False, "Эта группа уже настроена.")

            session.add(
                Group(
                    chat_id=group_id,
                    title=group_title[:255],
                    timezone=timezone,
                    anchor_monday=anchor_monday,
                    anchor_week_type=anchor_week_type,
                    created_by=user_id,
                )
            )
            session.add(
                GroupModerator(
                    group_id=group_id,
                    user_id=user_id,
                    role="owner",
                    display_name=display_name[:255],
                    added_by=user_id,
                )
            )
            session.add(
                GroupNotification(
                    group_id=group_id,
                    enabled=False,
                    notification_time="07:30",
                )
            )
            setup_code.used_at = utc_now()
            setup_code.used_by = user_id
            setup_code.used_group_id = group_id
            return CodeUseResult(
                True, "Группа успешно подключена.", setup_code.template_key
            )

    def get_group(self, group_id: int) -> Group | None:
        with self.Session() as session:
            return session.get(Group, group_id)

    def list_groups(self) -> list[Group]:
        with self.Session() as session:
            return list(
                session.scalars(select(Group).order_by(Group.created_at.desc()))
            )

    def update_group(self, group_id: int, **values: Any) -> Group | None:
        allowed = {
            "title",
            "timezone",
            "anchor_monday",
            "anchor_week_type",
            "welcome_text",
            "empty_day_text",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Нельзя изменить поля: {', '.join(sorted(unknown))}")
        with self.Session.begin() as session:
            group = session.get(Group, group_id)
            if group is None:
                return None
            for name, value in values.items():
                setattr(group, name, value)
            return group

    def get_role(self, group_id: int, user_id: int) -> str | None:
        with self.Session() as session:
            row = session.get(GroupModerator, (group_id, user_id))
            return row.role if row else None

    def list_moderators(self, group_id: int) -> list[GroupModerator]:
        with self.Session() as session:
            return list(
                session.scalars(
                    select(GroupModerator)
                    .where(GroupModerator.group_id == group_id)
                    .order_by(GroupModerator.role, GroupModerator.display_name)
                )
            )

    def add_moderator(
        self, group_id: int, user_id: int, display_name: str, added_by: int
    ) -> None:
        with self.Session.begin() as session:
            existing = session.get(GroupModerator, (group_id, user_id))
            if existing:
                existing.display_name = display_name[:255]
                return
            session.add(
                GroupModerator(
                    group_id=group_id,
                    user_id=user_id,
                    role="moderator",
                    display_name=display_name[:255],
                    added_by=added_by,
                )
            )

    def remove_moderator(self, group_id: int, user_id: int) -> bool:
        with self.Session.begin() as session:
            row = session.get(GroupModerator, (group_id, user_id))
            if row is None or row.role == "owner":
                return False
            session.delete(row)
            return True

    def transfer_ownership(
        self,
        group_id: int,
        target_user_id: int,
        target_display_name: str,
        changed_by: int,
    ) -> bool:
        with self.Session.begin() as session:
            current_owner = session.scalar(
                select(GroupModerator).where(
                    GroupModerator.group_id == group_id,
                    GroupModerator.role == "owner",
                )
            )
            if current_owner is None or current_owner.user_id == target_user_id:
                return False

            target = session.get(GroupModerator, (group_id, target_user_id))
            if target is None:
                target = GroupModerator(
                    group_id=group_id,
                    user_id=target_user_id,
                    role="owner",
                    display_name=target_display_name[:255],
                    added_by=changed_by,
                )
                session.add(target)
            else:
                target.role = "owner"
                target.display_name = target_display_name[:255]

            current_owner.role = "moderator"
            group = session.get(Group, group_id)
            if group is not None:
                group.created_by = target_user_id
            return True

    def list_subgroups(self, group_id: int) -> list[Subgroup]:
        with self.Session() as session:
            return list(
                session.scalars(
                    select(Subgroup)
                    .where(Subgroup.group_id == group_id)
                    .order_by(Subgroup.position, Subgroup.id)
                )
            )

    def get_subgroup(self, group_id: int, subgroup_id: int) -> Subgroup | None:
        with self.Session() as session:
            return session.scalar(
                select(Subgroup).where(
                    Subgroup.id == subgroup_id, Subgroup.group_id == group_id
                )
            )

    def add_subgroup(self, group_id: int, name: str) -> Subgroup:
        with self.Session.begin() as session:
            max_position = session.scalar(
                select(func.max(Subgroup.position)).where(Subgroup.group_id == group_id)
            )
            subgroup = Subgroup(
                group_id=group_id,
                name=name,
                position=(max_position or 0) + 1,
            )
            session.add(subgroup)
            return subgroup

    def rename_subgroup(self, group_id: int, subgroup_id: int, name: str) -> bool:
        with self.Session.begin() as session:
            subgroup = session.scalar(
                select(Subgroup).where(
                    Subgroup.id == subgroup_id, Subgroup.group_id == group_id
                )
            )
            if subgroup is None:
                return False
            subgroup.name = name
            return True

    def delete_subgroup(self, group_id: int, subgroup_id: int) -> bool:
        with self.Session.begin() as session:
            subgroup = session.scalar(
                select(Subgroup).where(
                    Subgroup.id == subgroup_id, Subgroup.group_id == group_id
                )
            )
            if subgroup is None:
                return False
            session.execute(
                delete(UserSubgroupPreference).where(
                    UserSubgroupPreference.group_id == group_id,
                    UserSubgroupPreference.subgroup_id == subgroup_id,
                )
            )
            session.execute(
                delete(ScheduleEntry).where(
                    ScheduleEntry.group_id == group_id,
                    ScheduleEntry.subgroup_id == subgroup_id,
                )
            )
            session.delete(subgroup)
            return True

    def set_user_subgroup(self, group_id: int, user_id: int, subgroup_id: int) -> bool:
        with self.Session.begin() as session:
            subgroup = session.scalar(
                select(Subgroup).where(
                    Subgroup.id == subgroup_id, Subgroup.group_id == group_id
                )
            )
            if subgroup is None:
                return False
            preference = session.get(UserSubgroupPreference, (group_id, user_id))
            if preference is None:
                session.add(
                    UserSubgroupPreference(
                        group_id=group_id,
                        user_id=user_id,
                        subgroup_id=subgroup_id,
                    )
                )
            else:
                preference.subgroup_id = subgroup_id
            return True

    def get_user_subgroup(self, group_id: int, user_id: int) -> Subgroup | None:
        with self.Session() as session:
            return session.scalar(
                select(Subgroup)
                .join(
                    UserSubgroupPreference,
                    UserSubgroupPreference.subgroup_id == Subgroup.id,
                )
                .where(
                    UserSubgroupPreference.group_id == group_id,
                    UserSubgroupPreference.user_id == user_id,
                    Subgroup.group_id == group_id,
                )
            )

    def clear_user_subgroup(self, group_id: int, user_id: int) -> None:
        with self.Session.begin() as session:
            preference = session.get(UserSubgroupPreference, (group_id, user_id))
            if preference is not None:
                session.delete(preference)

    def get_notification_settings(self, group_id: int) -> GroupNotification:
        with self.Session.begin() as session:
            notification = session.get(GroupNotification, group_id)
            if notification is None:
                notification = GroupNotification(
                    group_id=group_id,
                    enabled=False,
                    notification_time="07:30",
                )
                session.add(notification)
            return notification

    def update_notification_settings(
        self,
        group_id: int,
        *,
        enabled: bool | None = None,
        notification_time: str | None = None,
    ) -> GroupNotification:
        with self.Session.begin() as session:
            notification = session.get(GroupNotification, group_id)
            if notification is None:
                notification = GroupNotification(
                    group_id=group_id,
                    enabled=False,
                    notification_time="07:30",
                )
                session.add(notification)
            if enabled is not None:
                notification.enabled = enabled
            if notification_time is not None:
                notification.notification_time = notification_time
            return notification

    def list_enabled_notifications(self) -> list[tuple[Group, GroupNotification]]:
        with self.Session() as session:
            return list(
                session.execute(
                    select(Group, GroupNotification)
                    .join(
                        GroupNotification, GroupNotification.group_id == Group.chat_id
                    )
                    .where(Group.active.is_(True), GroupNotification.enabled.is_(True))
                ).all()
            )

    def claim_notification(self, group_id: int, local_date: date) -> bool:
        try:
            with self.Session.begin() as session:
                session.add(
                    NotificationDelivery(group_id=group_id, local_date=local_date)
                )
            return True
        except IntegrityError:
            return False

    def release_notification(self, group_id: int, local_date: date) -> None:
        with self.Session.begin() as session:
            session.execute(
                delete(NotificationDelivery).where(
                    NotificationDelivery.group_id == group_id,
                    NotificationDelivery.local_date == local_date,
                )
            )

    def list_schedule(
        self,
        group_id: int,
        week_type: str,
        day_of_week: int,
        subgroup_id: int | None = None,
    ) -> list[ScheduleEntry]:
        with self.Session() as session:
            return list(
                session.scalars(
                    select(ScheduleEntry)
                    .where(
                        ScheduleEntry.group_id == group_id,
                        ScheduleEntry.subgroup_id.is_(None)
                        if subgroup_id is None
                        else ScheduleEntry.subgroup_id == subgroup_id,
                        ScheduleEntry.week_type == week_type,
                        ScheduleEntry.day_of_week == day_of_week,
                    )
                    .order_by(ScheduleEntry.position, ScheduleEntry.id)
                )
            )

    def get_schedule_entry(self, group_id: int, entry_id: int) -> ScheduleEntry | None:
        with self.Session() as session:
            return session.scalar(
                select(ScheduleEntry).where(
                    ScheduleEntry.id == entry_id, ScheduleEntry.group_id == group_id
                )
            )

    def add_schedule_entry(
        self,
        group_id: int,
        week_type: str,
        day_of_week: int,
        text: str,
        subgroup_id: int | None = None,
    ) -> ScheduleEntry:
        with self.Session.begin() as session:
            if subgroup_id is not None:
                subgroup = session.scalar(
                    select(Subgroup).where(
                        Subgroup.id == subgroup_id, Subgroup.group_id == group_id
                    )
                )
                if subgroup is None:
                    raise ValueError("Подгруппа не найдена")
            max_position = session.scalar(
                select(func.max(ScheduleEntry.position)).where(
                    ScheduleEntry.group_id == group_id,
                    ScheduleEntry.subgroup_id.is_(None)
                    if subgroup_id is None
                    else ScheduleEntry.subgroup_id == subgroup_id,
                    ScheduleEntry.week_type == week_type,
                    ScheduleEntry.day_of_week == day_of_week,
                )
            )
            entry = ScheduleEntry(
                group_id=group_id,
                subgroup_id=subgroup_id,
                week_type=week_type,
                day_of_week=day_of_week,
                position=(max_position or 0) + 1,
                text=normalize_schedule_text(text),
            )
            session.add(entry)
            return entry

    def move_schedule_entry(
        self, group_id: int, entry_id: int, direction: str
    ) -> ScheduleEntry | None:
        if direction not in {"up", "down"}:
            raise ValueError("Неизвестное направление")
        with self.Session.begin() as session:
            entry = session.scalar(
                select(ScheduleEntry).where(
                    ScheduleEntry.id == entry_id, ScheduleEntry.group_id == group_id
                )
            )
            if entry is None:
                return None

            scope_filter = (
                ScheduleEntry.subgroup_id.is_(None)
                if entry.subgroup_id is None
                else ScheduleEntry.subgroup_id == entry.subgroup_id
            )
            position_filter = (
                ScheduleEntry.position < entry.position
                if direction == "up"
                else ScheduleEntry.position > entry.position
            )
            order = (
                ScheduleEntry.position.desc()
                if direction == "up"
                else ScheduleEntry.position.asc()
            )
            neighbour = session.scalar(
                select(ScheduleEntry)
                .where(
                    ScheduleEntry.group_id == group_id,
                    scope_filter,
                    ScheduleEntry.week_type == entry.week_type,
                    ScheduleEntry.day_of_week == entry.day_of_week,
                    position_filter,
                )
                .order_by(order)
                .limit(1)
            )
            if neighbour is None:
                return entry

            entry_position = entry.position
            neighbour_position = neighbour.position
            entry.position = -(entry.id + 1)
            session.flush()
            neighbour.position = entry_position
            session.flush()
            entry.position = neighbour_position
            return entry

    def update_schedule_entry(self, group_id: int, entry_id: int, text: str) -> bool:
        with self.Session.begin() as session:
            entry = session.scalar(
                select(ScheduleEntry).where(
                    ScheduleEntry.id == entry_id, ScheduleEntry.group_id == group_id
                )
            )
            if entry is None:
                return False
            entry.text = normalize_schedule_text(text)
            return True

    def delete_schedule_entry(self, group_id: int, entry_id: int) -> bool:
        with self.Session.begin() as session:
            entry = session.scalar(
                select(ScheduleEntry).where(
                    ScheduleEntry.id == entry_id, ScheduleEntry.group_id == group_id
                )
            )
            if entry is None:
                return False
            session.delete(entry)
            return True

    def replace_schedule_from_template(
        self, group_id: int, template: dict[str, dict[str, list[str]]]
    ) -> None:
        with self.Session.begin() as session:
            session.execute(
                delete(ScheduleEntry).where(ScheduleEntry.group_id == group_id)
            )
            for week_type, days in template.items():
                if week_type not in {"upper", "lower"}:
                    continue
                for day_key, entries in days.items():
                    day_of_week = int(day_key)
                    for position, text in enumerate(entries, start=1):
                        session.add(
                            ScheduleEntry(
                                group_id=group_id,
                                week_type=week_type,
                                day_of_week=day_of_week,
                                position=position,
                                text=normalize_schedule_text(text),
                            )
                        )

    def set_state(
        self,
        group_id: int,
        user_id: int,
        action: str,
        payload: dict[str, Any],
        prompt_message_id: int | None,
    ) -> None:
        with self.Session.begin() as session:
            state = session.get(ConversationState, (group_id, user_id))
            if state is None:
                state = ConversationState(
                    group_id=group_id, user_id=user_id, action=action
                )
                session.add(state)
            state.action = action
            state.payload_json = json.dumps(payload, ensure_ascii=False)
            state.prompt_message_id = prompt_message_id
            state.updated_at = utc_now()

    def get_state(self, group_id: int, user_id: int) -> StateData | None:
        with self.Session() as session:
            state = session.get(ConversationState, (group_id, user_id))
            if state is None:
                return None
            return StateData(
                action=state.action,
                payload=json.loads(state.payload_json),
                prompt_message_id=state.prompt_message_id,
            )

    def clear_state(self, group_id: int, user_id: int) -> None:
        with self.Session.begin() as session:
            state = session.get(ConversationState, (group_id, user_id))
            if state is not None:
                session.delete(state)
