from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit

from .delivery import Delivery
from .messages import extract_messages, message_number, parse_payload, request_peer, source_link
from .session import compact_session
from .memory import MemoryStore
from .store import Store


LOGGER = logging.getLogger(__name__)
WRITE_METHOD = re.compile(r"(?:messages\.(?:send|edit|delete|restore|addChatUser|removeChatUser|createChat)|"
                          r"wall\.(?:post|edit|delete)|act=(?:a_send|send|a_edit|a_delete))", re.I)
HISTORY_METHOD = re.compile(r"messages\.(?:getHistory|getByConversationMessageId|getById)(?:\b|\()", re.I)


@dataclass(frozen=True)
class RelaySettings:
    target_chat: int
    peer_id: int
    mention: str
    database_url: str
    session_file: str
    refresh: int = 120
    max_bytes: int = 45 * 1024 * 1024
    storage: str = "postgres"

    @classmethod
    def environment(cls, database_url: str):
        if os.getenv("VK_RELAY_ENABLED", "0").lower() not in {"1", "true", "yes"}:
            return None
        target = int(os.getenv("VK_TG_CHAT_ID", "0"))
        if target >= 0:
            raise ValueError("VK_TG_CHAT_ID must identify a Telegram group (negative integer)")
        peer = int(os.getenv("VK_PEER_ID", "2000000012"))
        if peer <= 2000000000:
            raise ValueError("VK_PEER_ID must identify the VK group conversation")
        mention = os.getenv("VK_REVIEW_MENTION", "@thelovelesss")
        if not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", mention):
            raise ValueError("Invalid VK_REVIEW_MENTION")
        url = os.getenv("VK_DATABASE_URL", database_url)
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        storage = os.getenv("VK_RELAY_STORAGE", "memory")
        if storage not in {"memory", "postgres"}:
            raise ValueError("VK_RELAY_STORAGE must be memory or postgres")
        if storage == "postgres" and os.getenv("RENDER") and url.startswith("sqlite"):
            raise ValueError("VK relay on Render needs persistent PostgreSQL; SQLite is ephemeral")
        refresh = int(os.getenv("VK_REFRESH_SECONDS", "120"))
        if not 30 <= refresh <= 900:
            raise ValueError("VK_REFRESH_SECONDS must be between 30 and 900")
        return cls(target, peer, mention, url,
                   os.getenv("VK_SESSION_FILE", "/etc/secrets/vk_auth.json"), refresh, storage=storage)


class VkRelay:
    def __init__(self, bot, settings: RelaySettings):
        self.settings = settings
        self.store = (MemoryStore() if settings.storage == "memory" else
                      Store(settings.database_url, f"{settings.peer_id}:{settings.target_chat}"))
        self.delivery = Delivery(bot, self.store, settings.target_chat, settings.peer_id,
                                 settings.mention, settings.max_bytes)
        self.stop = threading.Event()
        self.thread = None
        self.status = "starting"
        self.alert_times: dict[str, float] = {}
        self.force_reset = os.getenv("VK_SESSION_RESET", "0") == "1"
        self.test_lock = threading.Lock()
        self.test_reply_chat = None
        self.test_event = threading.Event()

    def request_test(self, reply_chat: int) -> bool:
        with self.test_lock:
            if self.test_reply_chat is not None:
                return False
            self.test_reply_chat = reply_chat
            self.test_event.set()
            return True

    def finish_test(self, success: bool):
        with self.test_lock:
            reply_chat = self.test_reply_chat
            self.test_reply_chat = None
            self.test_event.clear()
        if reply_chat is None or (success and reply_chat == self.settings.target_chat):
            return
        text = ("Последнее сообщение ВК отправлено в настроенную группу Telegram." if success else
                "Не удалось получить последнее сообщение ВК. Требуется проверка входа и подключения.")
        try:
            self.delivery.bot.send_message(reply_chat, text, parse_mode=None)
        except Exception:
            pass

    def start(self):
        self.thread = threading.Thread(target=self._thread_main, name="vk-relay", daemon=True)
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=10)

    def alert(self, key: str, text: str):
        if time.monotonic() - self.alert_times.get(key, -100000) < 3600:
            return
        self.delivery.text(f"{self.settings.mention} {text}\n{source_link(self.settings.peer_id)}")
        self.alert_times[key] = time.monotonic()

    def _thread_main(self):
        delay = 5
        while not self.stop.is_set():
            try:
                self.store.setup()
                with self.store.single_worker():
                    self.delivery.drain()
                    asyncio.run(self._browse())
                    self.finish_test(False)
                delay = 5
            except Exception as exc:
                self.finish_test(False)
                self.status = "retrying"
                # Browser/API/DB exceptions may include signed URLs or credentials.
                LOGGER.error("VK relay interrupted (%s); retrying", type(exc).__name__)
                try:
                    self.alert("interrupted", "Пересылка ВК прервана. Требуется ручная проверка; бот пробует подключиться снова.")
                except Exception:
                    pass
            self.stop.wait(delay)
            delay = min(delay * 2, 300)
        self.status = "stopped"

    async def _browse(self):
        # Imported only when enabled, so normal schedule deployments don't need
        # Playwright or a browser.
        from playwright.async_api import async_playwright

        initial = None if self.force_reset else self.store.load_session()
        if initial is None:
            try:
                initial = json.loads(Path(self.settings.session_file).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.status = "needs_login"
                self.alert("login", "Нужна сессия входа ВК. Пересылка ожидает настройки.")
                self.stop.wait(30)
                return
        queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        tasks: set[asyncio.Task] = set()
        seen_response = 0.0

        async def capture(response):
            nonlocal seen_response
            host = (urlsplit(response.url).hostname or "").lower()
            if not any(host == domain or host.endswith("." + domain) for domain in ("vk.ru", "vk.com")):
                return
            if response.request.resource_type not in {"xhr", "fetch"}:
                return
            request_description = unquote_plus(response.url + " " + (response.request.post_data or ""))
            # Conversation-list previews may be truncated. Only history responses
            # qualify as full messages; unknown client formats raise a stale alert.
            if not HISTORY_METHOD.search(request_description):
                return
            length = response.headers.get("content-length", "0")
            if length.isdigit() and int(length) > 8_000_000:
                return
            try:
                body = await response.body()
                if len(body) > 8_000_000:
                    return
                data = parse_payload(body.decode("utf-8"))
                messages = extract_messages(data, self.settings.peer_id,
                                            request_peer(response.url, response.request.post_data))
                if not seen_response:
                    LOGGER.info("VK history response: HTTP %s, parsed messages=%s",
                                response.status, len(messages))
                if messages:
                    if queue.full():
                        self.alert("overflow", "Очередь чтения ВК переполнена. Возможны пропуски; требуется ручная проверка.")
                    else:
                        queue.put_nowait((time.monotonic(), messages))
                    seen_response = time.monotonic()
            except (ValueError, UnicodeError, RecursionError):
                return
            except Exception:
                # Aborted responses during reload are normal. Persistent loss of
                # readable responses is reported by the stale timer below.
                return

        def on_response(response):
            task = asyncio.create_task(capture(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        async def guard(route):
            request = route.request
            text = unquote_plus(request.url + " " + (request.post_data or ""))
            if WRITE_METHOD.search(text):
                await route.abort()
            elif request.resource_type in {"image", "media", "font"}:
                await route.abort()
            else:
                await route.continue_()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=[
                "--disable-dev-shm-usage", "--disable-gpu", "--disable-software-rasterizer",
                "--single-process", "--no-zygote", "--js-flags=--max-old-space-size=128",
            ])
            context = await browser.new_context(storage_state=initial, service_workers="block",
                                                locale="ru-RU", viewport={"width": 1000, "height": 750})
            await context.route("**/*", guard)
            page = await context.new_page()
            page.on("response", on_response)
            page.on("crash", lambda _: LOGGER.warning("VK browser page crashed"))
            try:
                await page.goto(source_link(self.settings.peer_id), wait_until="commit", timeout=60000)
                LOGGER.info("VK page opened on host=%s", urlsplit(page.url).hostname)
                started = last_reload = time.monotonic()
                last_save = 0.0
                test_started = None
                page_checked = False
                while not self.stop.is_set():
                    self.store.ensure_lock()
                    try:
                        received_at, messages = await asyncio.wait_for(queue.get(), timeout=2)
                        test_messages = list(messages) if test_started is not None and received_at >= test_started else []
                        # Merge nearby responses before advancing the checkpoint.
                        await asyncio.sleep(0.5)
                        combined = {tuple((m.get("conversation_message_id"), m.get("id"))): m for m in messages}
                        while not queue.empty():
                            batch_time, batch = queue.get_nowait()
                            if test_started is not None and batch_time >= test_started:
                                test_messages.extend(batch)
                            for item in batch:
                                combined[(item.get("conversation_message_id"), item.get("id"))] = item
                        baseline, gap = self.store.observe(list(combined.values()))
                        self.status = "reading"
                        if baseline:
                            self.delivery.text("Чтение ВК подключено. Уже загруженная история пропущена; пересылаю новые сообщения.")
                        if gap:
                            self.alert("gap", "После перерыва история ВК не перекрывает сохранённую позицию. Возможны пропущенные сообщения; требуется ручная проверка.")
                        if test_messages:
                            latest = max(test_messages, key=lambda item: message_number(item)[1])
                            await asyncio.to_thread(self.delivery.test_message, latest)
                            self.finish_test(True)
                            test_started = None
                    except asyncio.TimeoutError:
                        pass
                    # Sending can take time; keep the browser event loop running.
                    await asyncio.to_thread(self.delivery.drain)
                    now = time.monotonic()
                    if not page_checked and now - started > 30 and not seen_response:
                        page_checked = True
                        try:
                            visible = (await page.locator("body").inner_text(timeout=3000)).lower()
                            LOGGER.info("VK page diagnostic: host=%s, text_length=%s, login=%s, captcha=%s",
                                        urlsplit(page.url).hostname, len(visible),
                                        any(word in visible for word in ("войти в вк", "телефон или почта", "войти или зарегистрироваться")),
                                        any(word in visible for word in ("captcha", "капч", "я не робот")))
                        except Exception as exc:
                            LOGGER.warning("VK page diagnostic unavailable (%s)", type(exc).__name__)
                    if seen_response and now - last_save > 60 and now - seen_response < 180:
                        self.store.save_session(compact_session(await context.storage_state(indexed_db=True)))
                        self.force_reset = False
                        last_save = now
                    if now - (seen_response or started) > max(180, self.settings.refresh * 2):
                        self.status = "needs_review"
                        self.alert("unreadable", "Не удалось прочитать сообщения ВК: вход истёк, нет доступа или изменился формат страницы. Требуется ручная проверка.")
                    if test_started is not None and now - test_started > 60:
                        self.finish_test(False)
                        test_started = None
                    if self.test_event.is_set() and test_started is None:
                        self.test_event.clear()
                        test_started = time.monotonic()
                        await page.reload(wait_until="domcontentloaded", timeout=60000)
                        last_reload = time.monotonic()
                    elif now - last_reload >= self.settings.refresh:
                        await page.reload(wait_until="domcontentloaded", timeout=60000)
                        last_reload = time.monotonic()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await browser.close()
