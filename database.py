from __future__ import annotations

import hashlib
import json
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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utc_now() -> datetime:
    # Naive UTC keeps SQLite and PostgreSQL comparisons consistent.
    return datetime.now(UTC).replace(tzinfo=None)


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


class ScheduleEntry(Base):
    __tablename__ = "schedule_entries"
    __table_args__ = (
        UniqueConstraint(
            "group_id",
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
    week_type: Mapped[str] = mapped_column(String(5), index=True)
    day_of_week: Mapped[int] = mapped_column(Integer, index=True)
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)


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

    def list_schedule(
        self, group_id: int, week_type: str, day_of_week: int
    ) -> list[ScheduleEntry]:
        with self.Session() as session:
            return list(
                session.scalars(
                    select(ScheduleEntry)
                    .where(
                        ScheduleEntry.group_id == group_id,
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
        self, group_id: int, week_type: str, day_of_week: int, text: str
    ) -> ScheduleEntry:
        with self.Session.begin() as session:
            max_position = session.scalar(
                select(func.max(ScheduleEntry.position)).where(
                    ScheduleEntry.group_id == group_id,
                    ScheduleEntry.week_type == week_type,
                    ScheduleEntry.day_of_week == day_of_week,
                )
            )
            entry = ScheduleEntry(
                group_id=group_id,
                week_type=week_type,
                day_of_week=day_of_week,
                position=(max_position or 0) + 1,
                text=text,
            )
            session.add(entry)
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
            entry.text = text
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
                                text=text,
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
