from __future__ import annotations

import re
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from telebot.apihelper import ApiTelegramException

from .messages import Part, chunks, plan_message, warning


CDN_DOMAINS = (
    "userapi.com", "vk.com", "vk.ru", "vk-cdn.net", "vkuseraudio.net",
    "vkuseraudio.ru", "vkuserphoto.ru", "vkuser.net", "vkvideo.ru", "mycdn.me",
)


def allowed_media_url(url: str) -> bool:
    try:
        value = urlsplit(url)
        host = (value.hostname or "").lower()
        return (value.scheme == "https" and value.port in (None, 443) and
                not value.username and not value.password and
                any(host == suffix or host.endswith("." + suffix) for suffix in CDN_DOMAINS))
    except (ValueError, TypeError):
        return False


def download(url: str, target: Path, max_bytes: int) -> None:
    # Do not send VK browser cookies to file hosts. Signed CDN URLs normally
    # suffice; protected files produce a manual-review notice instead.
    for _ in range(6):
        if not allowed_media_url(url):
            raise ValueError("Unsupported media host")
        with requests.get(url, stream=True, allow_redirects=False, timeout=(10, 45)) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            response.raise_for_status()
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise ValueError("File exceeds limit")
            total = 0
            with target.open("wb") as handle:
                for block in response.iter_content(64 * 1024):
                    total += len(block)
                    if total > max_bytes:
                        raise ValueError("File exceeds limit")
                    handle.write(block)
            return
    raise ValueError("Too many file redirects")


class Delivery:
    def __init__(self, bot, store, chat_id: int, peer_id: int, mention: str, max_bytes: int):
        self.bot, self.store = bot, store
        self.chat_id, self.peer_id, self.mention = chat_id, peer_id, mention
        self.max_bytes = max_bytes

    def text(self, value: str) -> None:
        self.bot.send_message(self.chat_id, value, parse_mode=None, disable_web_page_preview=True)

    def send_part(self, part: Part, number: int) -> None:
        if part.kind == "text":
            self.text(part.text)
            return
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", part.filename)[:150].strip(" .") or "file"
        try:
            with tempfile.TemporaryDirectory(prefix="vk-relay-") as temp:
                path = Path(temp) / filename
                download(part.url, path, self.max_bytes)
                with path.open("rb") as handle:
                    method = {
                        "photo": self.bot.send_photo, "document": self.bot.send_document,
                        "voice": self.bot.send_voice, "audio": self.bot.send_audio,
                        "video": self.bot.send_video,
                    }[part.kind]
                    method(self.chat_id, handle, timeout=60)
        except ApiTelegramException as exc:
            # Rate limits, auth/permission failures and server failures are retried
            # by the worker, preserving the current part. A bad media format isn't.
            if exc.error_code not in (400, 413):
                raise
            self.text(warning(self.peer_id, number, self.mention,
                              f"Telegram не принял вложение ({part.kind})"))
        except (requests.RequestException, ValueError, OSError):
            self.text(warning(self.peer_id, number, self.mention,
                              f"вложение недоступно, слишком велико или не удалось скачать ({part.kind})"))

    def drain(self) -> None:
        for number, message, next_part in self.store.pending():
            try:
                parts = plan_message(message, self.peer_id, self.mention)
            except (ValueError, TypeError, KeyError, AttributeError):
                # A future/unrecognised VK object must not block every later
                # message. Preserve any text and explicitly request review.
                parts = [Part("text", text=part) for part in chunks(str(message.get("text") or ""))]
                parts.append(Part("text", text=warning(self.peer_id, number, self.mention,
                                                       "неизвестная структура сообщения")))
            for index in range(next_part, len(parts)):
                self.store.ensure_lock()
                self.send_part(parts[index], number)
                self.store.checkpoint(number, index + 1)
            self.store.complete(number)
