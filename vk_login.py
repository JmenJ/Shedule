"""User-operated login helper. Never prints or uploads account credentials."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.async_api import async_playwright
from vk_relay.messages import extract_messages, parse_payload, request_peer, source_link
from vk_relay.service import HISTORY_METHOD
from vk_relay.session import compact_session

ROOT = Path(__file__).resolve().parent
PEER = 2000000012


def shape(value, depth=0):
    """Diagnostic schema contains types/field names, never actual field values."""
    if depth > 7:
        return type(value).__name__
    if isinstance(value, dict):
        return {(str(key) if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", str(key)) else "<dynamic-key>"):
                shape(item, depth + 1) for key, item in list(value.items())[:35]}
    if isinstance(value, list):
        return [shape(item, depth + 1) for item in value[:1]]
    return type(value).__name__


async def main():
    folder = ROOT / "vk-session-private"
    folder.mkdir(exist_ok=True)
    found = set()
    diagnostics = []
    tasks = set()

    async def capture(response):
        host = (urlsplit(response.url).hostname or "").lower()
        if not any(host == item or host.endswith("." + item) for item in ("vk.ru", "vk.com")):
            return
        if response.request.resource_type not in {"xhr", "fetch"}:
            return
        try:
            raw = await response.body()
            if len(raw) > 8_000_000:
                return
            data = parse_payload(raw.decode("utf-8"))
            description = unquote_plus(response.url + " " + (response.request.post_data or ""))
            if HISTORY_METHOD.search(description):
                for message in extract_messages(data, PEER, request_peer(response.url, response.request.post_data)):
                    found.add(message.get("conversation_message_id") or message.get("id"))
            if len(diagnostics) < 20:
                path = urlsplit(response.url).path
                if not re.fullmatch(r"/(?:method/)?[A-Za-z_.]+", path):
                    path = "/<endpoint>"
                diagnostics.append({"path": path, "shape": shape(data)})
        except Exception:
            pass

    def on_response(response):
        task = asyncio.create_task(capture(response))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(locale="ru-RU", service_workers="block")
        page = await context.new_page()
        page.on("response", on_response)
        print("Войдите в ВК в открывшемся браузере. Пароль и код вводите только на сайте ВК.")
        print("Откройте нужную беседу. Когда она загрузится, вернитесь сюда и нажмите Enter.")
        await page.goto(source_link(PEER), wait_until="domcontentloaded", timeout=60000)
        await asyncio.to_thread(input)
        # Reopen the configured conversation to verify the exact reader used by
        # the server, rather than merely trusting the URL of a login page.
        await page.goto(source_link(PEER), wait_until="domcontentloaded", timeout=60000)
        for _ in range(30):
            if found:
                break
            await asyncio.sleep(1)
        if not found:
            diagnostic_file = folder / "vk_format_diagnostic.json"
            diagnostic_file.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
            print("Формат сообщений не распознан. Сессия НЕ сохранена как готовая к работе.")
            print("Диагностика с названиями полей без их значений:", diagnostic_file)
        else:
            path = folder / "vk_auth.json"
            temp = path.with_suffix(".tmp")
            state = compact_session(await context.storage_state(indexed_db=True))
            temp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(temp, path)
            print(f"Чтение нужной беседы подтверждено: распознано {len(found)} сообщений.")
            print("Сессия сохранена:", path)
            print("Не присылайте vk_auth.json в чат и не добавляйте его в GitHub.")
            print("На Render файл добавляется в Environment > Secret Files как vk_auth.json.")
            print("Перенос сессии на сервер ещё требует отдельной проверки.")
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Отменено.")
    except Exception as exc:
        print("Вход не завершён:", type(exc).__name__)
    input("Нажмите Enter для закрытия...")
