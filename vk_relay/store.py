from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager

from sqlalchemy import BigInteger, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .messages import message_number


class Base(DeclarativeBase):
    pass


class RelayState(Base):
    __tablename__ = "vk_relay_state"
    namespace: Mapped[str] = mapped_column(String(120), primary_key=True)
    cursor: Mapped[int] = mapped_column(BigInteger, default=0)
    id_kind: Mapped[str] = mapped_column(String(40), default="")
    session_json: Mapped[str] = mapped_column(Text, default="")


class Outbox(Base):
    __tablename__ = "vk_relay_outbox"
    namespace: Mapped[str] = mapped_column(String(120), primary_key=True)
    number: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    next_part: Mapped[int] = mapped_column(Integer, default=0)


class Store:
    def __init__(self, url: str, namespace: str):
        self.engine = create_engine(url, pool_pre_ping=True)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.namespace = namespace
        self.lock_connection = None

    def setup(self) -> None:
        Base.metadata.create_all(self.engine)
        with self.sessions.begin() as db:
            if db.get(RelayState, self.namespace) is None:
                db.add(RelayState(namespace=self.namespace))

    @contextmanager
    def single_worker(self):
        """Postgres protects against overlapping Render deployments.

        SQLite is local-development-only on Render; local runs must use one process.
        """
        connection = self.engine.connect()
        self.lock_connection = connection
        key = int.from_bytes(hashlib.sha256(self.namespace.encode()).digest()[:8], "big", signed=True)
        try:
            if self.engine.dialect.name == "postgresql":
                acquired = connection.exec_driver_sql("SELECT pg_try_advisory_lock(%s)", (key,)).scalar()
                if not acquired:
                    raise RuntimeError("Another VK relay worker is active")
            yield
        finally:
            self.lock_connection = None
            # Closing the connection releases the session lock; do not return a
            # still-locked connection to the pool.
            if self.engine.dialect.name == "postgresql":
                try:
                    connection.exec_driver_sql("SELECT pg_advisory_unlock(%s)", (key,))
                except Exception:
                    connection.invalidate()
            connection.close()

    def ensure_lock(self) -> None:
        if self.lock_connection is None:
            raise RuntimeError("Relay has no worker lock")
        # If the connection died, SQLAlchemy may reconnect. Never silently
        # reconnect a session-scoped advisory lock.
        if self.lock_connection.invalidated or self.lock_connection.closed:
            raise RuntimeError("Relay worker lock was lost")
        self.lock_connection.exec_driver_sql("SELECT 1")

    def state(self) -> tuple[str, int]:
        with self.sessions() as db:
            row = db.get(RelayState, self.namespace)
            return row.id_kind, row.cursor

    def observe(self, messages: list[dict]) -> tuple[bool, bool]:
        if not messages:
            return False, False
        kinds = {message_number(message)[0] for message in messages}
        if len(kinds) != 1:
            raise ValueError("Inconsistent VK identifiers; refusing to mix message streams")
        kind = kinds.pop()
        messages = sorted(messages, key=lambda item: message_number(item)[1])
        with self.sessions.begin() as db:
            row = db.get(RelayState, self.namespace)
            if not row.id_kind:
                row.id_kind, row.cursor = kind, message_number(messages[-1])[1]
                return True, False
            if row.id_kind != kind:
                raise ValueError("VK message identifier format changed")
            numbers = [message_number(item)[1] for item in messages]
            pending = set(db.scalars(select(Outbox.number).where(Outbox.namespace == self.namespace)))
            new = [item for item in messages if message_number(item)[1] > row.cursor]
            # Full history may no longer overlap our checkpoint after an outage.
            # A consecutive conversation id is enough evidence that nothing was skipped.
            gap = bool(new and row.cursor not in numbers and not pending and
                       not (kind == "conversation_message_id" and numbers[0] == row.cursor + 1))
            for item in new:
                number = message_number(item)[1]
                if number not in pending:
                    db.add(Outbox(namespace=self.namespace, number=number,
                                  payload=json.dumps(item, ensure_ascii=False)))
                    pending.add(number)
            return False, gap

    def pending(self) -> list[tuple[int, dict, int]]:
        with self.sessions() as db:
            return [(item.number, json.loads(item.payload), item.next_part)
                    for item in db.scalars(select(Outbox).where(Outbox.namespace == self.namespace)
                                           .order_by(Outbox.number))]

    def checkpoint(self, number: int, next_part: int) -> None:
        with self.sessions.begin() as db:
            item = db.get(Outbox, (self.namespace, number))
            item.next_part = next_part

    def complete(self, number: int) -> None:
        with self.sessions.begin() as db:
            row = db.get(RelayState, self.namespace)
            row.cursor = max(row.cursor, number)
            item = db.get(Outbox, (self.namespace, number))
            if item is not None:
                db.delete(item)

    def load_session(self) -> dict | None:
        with self.sessions() as db:
            value = db.get(RelayState, self.namespace).session_json
            return json.loads(value) if value else None

    def save_session(self, value: dict) -> None:
        encoded = json.dumps(value, separators=(",", ":"))
        with self.sessions.begin() as db:
            row = db.get(RelayState, self.namespace)
            if row.session_json != encoded:
                row.session_json = encoded
