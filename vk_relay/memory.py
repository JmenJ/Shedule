from contextlib import contextmanager
import json
import threading

from .messages import message_number


class MemoryStore:
    """Real-time mode: progress and pending messages last until process exit."""
    def __init__(self):
        self.kind, self.cursor = "", 0
        self.items = {}
        self.session = None
        self.worker_lock = threading.Lock()

    def setup(self):
        pass

    @contextmanager
    def single_worker(self):
        if not self.worker_lock.acquire(blocking=False):
            raise RuntimeError("Another relay worker is active")
        try:
            yield
        finally:
            self.worker_lock.release()

    def ensure_lock(self):
        if not self.worker_lock.locked():
            raise RuntimeError("Relay worker lock was lost")

    def state(self):
        return self.kind, self.cursor

    def observe(self, messages):
        if not messages:
            return False, False
        kinds = {message_number(item)[0] for item in messages}
        if len(kinds) != 1:
            raise ValueError("Inconsistent message identifiers")
        kind = kinds.pop()
        ordered = sorted(messages, key=lambda item: message_number(item)[1])
        numbers = [message_number(item)[1] for item in ordered]
        if not self.kind:
            self.kind, self.cursor = kind, numbers[-1]
            return True, False
        if self.kind != kind:
            raise ValueError("Message identifier format changed")
        new = [item for item in ordered if message_number(item)[1] > self.cursor]
        gap = bool(new and self.cursor not in numbers and not self.items and
                   not (kind == "conversation_message_id" and numbers[0] == self.cursor + 1))
        for item in new:
            self.items.setdefault(message_number(item)[1], [json.loads(json.dumps(item)), 0])
        return False, gap

    def pending(self):
        return [(number, item[0], item[1]) for number, item in sorted(self.items.items())]

    def checkpoint(self, number, next_part):
        self.items[number][1] = next_part

    def complete(self, number):
        self.cursor = max(self.cursor, number)
        self.items.pop(number, None)

    def load_session(self):
        return self.session

    def save_session(self, value):
        self.session = value
