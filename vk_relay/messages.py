from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class Part:
    kind: str
    text: str = ""
    url: str = ""
    filename: str = "file"


def source_link(peer_id: int) -> str:
    return f"https://vk.ru/im/convo/{peer_id}"


def message_number(message: dict) -> tuple[str, int]:
    for key in ("conversation_message_id", "id"):
        value = message.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return key, value
    raise ValueError("No stable message identifier")


def parse_payload(body: str) -> Any:
    body = body.strip()
    for prefix in ("for (;;);", "for(;;);", ")]}'"):
        if body.startswith(prefix):
            body = body[len(prefix):].lstrip(";\r\n ")
    return json.loads(body)


def request_peer(url: str, post_data: str | None) -> int | None:
    parameters = parse_qs(urlsplit(url).query)
    parameters.update(parse_qs(post_data or ""))
    values = parameters.get("peer_id", [])
    if len(values) == 1 and values[0].isdigit():
        return int(values[0])
    return None


def extract_messages(payload: Any, peer_id: int, implicit_peer: int | None = None) -> list[dict]:
    """Read structured responses produced by the web client, never browser tokens.

    Forwarded/replied messages belong to their outer message, not the inbox.
    Responses without an explicit peer or a matching request peer are ignored.
    """
    found: dict[tuple[str, int], dict] = {}
    budget = 30000

    def visit(node: Any, inherited_peer: int | None, depth: int) -> None:
        nonlocal budget
        budget -= 1
        if budget < 0 or depth > 25:
            return
        if isinstance(node, dict):
            actual_peer = node.get("peer_id", inherited_peer)
            if actual_peer == peer_id and "from_id" in node and "date" in node:
                try:
                    key = message_number(node)
                except ValueError:
                    pass
                else:
                    found[key] = {**node, "peer_id": peer_id}
                    return
            for key, value in node.items():
                if key not in {"reply_message", "fwd_messages", "attachments"}:
                    visit(value, actual_peer, depth + 1)
        elif isinstance(node, list):
            for item in node:
                visit(item, inherited_peer, depth + 1)
        elif isinstance(node, str) and len(node) < 2_000_000 and node.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(node)
            except (ValueError, RecursionError):
                return
            visit(decoded, inherited_peer, depth + 1)

    visit(payload, implicit_peer, 0)
    return sorted(found.values(), key=lambda item: message_number(item)[1])


def chunks(text: str, limit: int = 3500) -> list[str]:
    result, current, units = [], [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit:
            result.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += size
    if current:
        result.append("".join(current))
    return result


def warning(peer_id: int, number: int, mention: str, reason: str) -> str:
    return (f"{mention} Не удалось перенести часть сообщения ВК №{number}: "
            f"{reason}. Требуется ручная проверка.\n{source_link(peer_id)}")


def plan_message(message: dict, peer_id: int, mention: str) -> list[Part]:
    number = message_number(message)[1]
    parts: list[Part] = []
    unsupported: set[str] = set()

    def text(value: str) -> None:
        parts.extend(Part("text", text=chunk) for chunk in chunks(value))

    def visit(item: dict, depth: int = 0) -> None:
        if depth > 5:
            unsupported.add("слишком много уровней пересылки")
            return
        if item.get("text"):
            text(str(item["text"]))
        if item.get("action"):
            unsupported.add("служебное событие")
        if item.get("geo"):
            unsupported.add("геолокация")
        for attachment in (item.get("attachments") or []):
            if not isinstance(attachment, dict):
                unsupported.add("неизвестное вложение")
                continue
            kind = attachment.get("type", "неизвестное вложение")
            data = attachment.get(kind, {})
            if not isinstance(data, dict):
                unsupported.add(str(kind))
                continue
            url = ""
            if kind == "photo":
                sizes = [s for s in (data.get("sizes") or []) if isinstance(s, dict) and s.get("url")]
                original = data.get("orig_photo") or {}
                if sizes:
                    url = max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))["url"]
                elif isinstance(original, dict) and original.get("url"):
                    url = original["url"]
                if url:
                    parts.append(Part("photo", url=url, filename="photo.jpg"))
            elif kind == "doc":
                url = data.get("url", "")
                if url:
                    parts.append(Part("document", url=url, filename=str(data.get("title") or "file")))
            elif kind == "audio_message":
                url = data.get("link_ogg") or data.get("link_mp3", "")
                if url:
                    parts.append(Part("voice" if data.get("link_ogg") else "audio", url=url,
                                      filename="voice.ogg" if data.get("link_ogg") else "voice.mp3"))
            elif kind == "audio":
                url = data.get("url", "")
                if url:
                    parts.append(Part("audio", url=url, filename="audio.mp3"))
            elif kind == "video":
                files = data.get("files") or {}
                if not isinstance(files, dict):
                    files = {}
                choices = [(int(k[4:]), v) for k, v in files.items()
                           if k.startswith("mp4_") and k[4:].isdigit() and isinstance(v, str)]
                if choices:
                    url = max(choices)[1]
                    parts.append(Part("video", url=url, filename="video.mp4"))
            elif kind == "link" and data.get("url"):
                url = str(data["url"])
                text(f"{data.get('title', '')}\n{url}".strip())
            if not url:
                unsupported.add(str(kind))
        reply = item.get("reply_message")
        if isinstance(reply, dict):
            text("Ответ:")
            visit(reply, depth + 1)
        for forwarded in (item.get("fwd_messages") or []):
            if isinstance(forwarded, dict):
                text("Пересланное сообщение:")
                visit(forwarded, depth + 1)

    visit(message)
    if not message.get("text") and not message.get("attachments") and not message.get("action") and not message.get("fwd_messages"):
        unsupported.add("пустое или неизвестное содержимое")
    if unsupported:
        text(warning(peer_id, number, mention, ", ".join(sorted(unsupported))))
    return parts
