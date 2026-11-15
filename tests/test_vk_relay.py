from __future__ import annotations

import json
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from vk_relay.delivery import Delivery, allowed_media_url, download
from vk_relay.messages import chunks, extract_messages, parse_payload, plan_message, request_peer
from vk_relay.service import RelaySettings, VkRelay, WRITE_METHOD, HISTORY_METHOD
from vk_relay.store import Store


PEER = 2000000012


def message(number, **kwargs):
    return {"conversation_message_id": number, "id": number + 10000,
            "date": 1700000000 + number, "from_id": 123,
            "peer_id": PEER, "text": f"Message {number}", **kwargs}


class MessageTests(TestCase):
    def test_foreign_chat_and_nested_replies_not_mistaken_for_new_messages(self):
        own = message(10, reply_message=message(9), fwd_messages=[message(8)])
        other = message(20, peer_id=2000000020)
        result = extract_messages({"response": {"items": [own, other]}}, PEER)
        self.assertEqual([m["conversation_message_id"] for m in result], [10])

    def test_implicit_peer_requires_explicit_matching_request(self):
        item = message(10)
        item.pop("peer_id")
        self.assertEqual(extract_messages({"items": [item]}, PEER), [])
        self.assertEqual(len(extract_messages({"items": [item]}, PEER, PEER)), 1)
        self.assertIsNone(request_peer("https://api.vk.ru/method/execute", "code=secret"))
        self.assertEqual(request_peer("https://api.vk.ru/method/messages.getHistory", f"peer_id={PEER}"), PEER)

    def test_legacy_wrapper_and_encoded_response(self):
        raw = "for (;;);" + json.dumps({"payload": [0, json.dumps({"items": [message(3)]})]})
        self.assertEqual(extract_messages(parse_payload(raw), PEER)[0]["conversation_message_id"], 3)

    def test_photo_largest_document_and_poll_notification(self):
        item = message(1, attachments=[
            {"type": "photo", "photo": {"sizes": [
                {"url": "https://sun.userapi.com/s.jpg", "width": 10, "height": 10},
                {"url": "https://sun.userapi.com/l.jpg", "width": 100, "height": 100}]}},
            {"type": "doc", "doc": {"url": "https://vk.ru/doc1", "title": "lesson.pdf"}},
            {"type": "poll", "poll": {"question": "Выбор?"}},
        ])
        parts = plan_message(item, PEER, "@thelovelesss")
        self.assertEqual([p.kind for p in parts], ["text", "text", "photo", "document", "text"])
        self.assertTrue(parts[2].url.endswith("/l.jpg"))
        self.assertIn("@thelovelesss", parts[-1].text)
        self.assertIn("poll", parts[-1].text)
        self.assertIn("Требуется ручная проверка", parts[-1].text)

    def test_inaccessible_video_and_unknown_are_not_silently_ignored(self):
        parts = plan_message(message(1, text="", attachments=[
            {"type": "video", "video": {"player": "https://vkvideo.ru/player"}},
            {"type": "new_future_format", "new_future_format": {}}]), PEER, "@thelovelesss")
        self.assertIn("video", parts[-1].text)
        self.assertIn("new_future_format", parts[-1].text)

    def test_unicode_chunks_preserve_every_character(self):
        text = "😀<>&" * 5000
        result = chunks(text)
        self.assertEqual("".join(result), text)
        self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 3500 for part in result))

    def test_history_filter_rejects_truncated_conversation_previews(self):
        self.assertFalse(HISTORY_METHOD.search("/method/messages.getConversations"))
        self.assertTrue(HISTORY_METHOD.search("code=API.messages.getHistory({})"))

    def test_write_guard_blocks_vk_send_and_legacy_form(self):
        for value in ("/method/messages.send", "API.messages.edit({})", "act=a_send&msg=hi"):
            self.assertIsNotNone(WRITE_METHOD.search(value))
        self.assertIsNone(WRITE_METHOD.search("/method/messages.getHistory"))


class StoreTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.url = "sqlite:///" + str(Path(self.temp.name) / "test.db")
        self.store = Store(self.url, "test")
        self.store.setup()

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_baseline_skips_history_then_new_messages_are_ordered_and_deduplicated(self):
        self.assertEqual(self.store.observe([message(9), message(10)]), (True, False))
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.observe([message(12), message(10), message(11)]), (False, False))
        self.store.observe([message(11), message(12)])
        self.assertEqual([r[0] for r in self.store.pending()], [11, 12])

    def test_progress_and_session_survive_new_process_store(self):
        self.store.observe([message(10)])
        self.store.observe([message(11)])
        self.store.checkpoint(11, 2)
        self.store.save_session({"cookies": [{"name": "redacted"}], "origins": []})
        restored = Store(self.url, "test")
        restored.setup()
        self.assertEqual(restored.pending()[0][2], 2)
        self.assertEqual(restored.load_session()["cookies"][0]["name"], "redacted")
        restored.complete(11)
        self.assertEqual(restored.state()[1], 11)
        self.assertEqual(restored.pending(), [])
        restored.engine.dispose()

    def test_gap_reported_after_outage_and_wrong_identifier_refused(self):
        self.store.observe([message(10)])
        self.assertTrue(self.store.observe([message(20), message(21)])[1])
        with self.assertRaises(ValueError):
            self.store.observe([{"id": 100, "from_id": 1, "date": 1, "peer_id": PEER}])


class FakeBot:
    def __init__(self):
        self.sent = []
        self.fail_text = None

    def send_message(self, chat, text, **kwargs):
        if self.fail_text == text:
            raise RuntimeError("temporary failure")
        self.sent.append(text)

    def send_photo(self, *args, **kwargs):
        self.sent.append("PHOTO")

    send_document = send_photo
    send_audio = send_photo
    send_voice = send_photo
    send_video = send_photo


class DeliveryTests(TestCase):
    setUp = StoreTests.setUp
    tearDown = StoreTests.tearDown
    def test_resume_does_not_repeat_completed_parts(self):
        self.store.observe([message(1)])
        self.store.observe([message(2, text="body")])
        bot = FakeBot()
        bot.fail_text = "body"
        delivery = Delivery(bot, self.store, -100, PEER, "@thelovelesss", 1024)
        with self.store.single_worker():
            with self.assertRaises(RuntimeError):
                delivery.drain()
            self.assertEqual(len(bot.sent), 1)
            bot.fail_text = None
            delivery.drain()
        self.assertEqual(bot.sent[-1], "body")
        self.assertEqual(len(bot.sent), 2)
        self.assertEqual(self.store.pending(), [])

    def test_media_failure_sends_mention_and_advances_after_notice(self):
        bot = FakeBot()
        delivery = Delivery(bot, self.store, -100, PEER, "@thelovelesss", 1024)
        parts = plan_message(message(2, attachments=[{"type": "doc", "doc": {
            "url": "https://vk.ru/doc1", "title": "x.pdf"}}]), PEER, "@thelovelesss")
        with patch("vk_relay.delivery.download", side_effect=ValueError("too large")):
            delivery.send_part(parts[-1], 2)
        self.assertIn("@thelovelesss", bot.sent[-1])
        self.assertIn("ручная проверка", bot.sent[-1])

    def test_download_host_policy_rejects_private_and_lookalike_hosts(self):
        for url in ("http://vk.ru/a", "https://127.0.0.1/a", "https://vk.ru.evil.test/a",
                    "https://vk.ru@evil.test/a", "https://vk.ru:8443/a"):
            self.assertFalse(allowed_media_url(url), url)
        self.assertTrue(allowed_media_url("https://sun9.userapi.com/a.jpg"))

    def test_redirect_and_stream_size_are_checked(self):
        class Response:
            is_redirect = True
            headers = {"Location": "http://127.0.0.1/secret"}
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with patch("vk_relay.delivery.requests.get", return_value=Response()) as get:
            with self.assertRaises(ValueError):
                download("https://vk.ru/doc1", Path(self.temp.name) / "x", 1024)
            self.assertEqual(get.call_count, 1)
        class Stream(Response):
            is_redirect = False
            headers = {}
            def raise_for_status(self): pass
            def iter_content(self, size): return iter([b"a" * 6, b"b" * 6])
        with patch("vk_relay.delivery.requests.get", return_value=Stream()):
            with self.assertRaises(ValueError):
                download("https://vk.ru/doc1", Path(self.temp.name) / "x", 10)


class ConfigurationTests(TestCase):
    def test_disabled_by_default_and_render_rejects_ephemeral_state(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(RelaySettings.environment("sqlite:///test.db"))
        with patch.dict("os.environ", {"VK_RELAY_ENABLED": "1", "VK_TG_CHAT_ID": "-100", "RENDER": "true"}, clear=True):
            with self.assertRaisesRegex(ValueError, "persistent"):
                RelaySettings.environment("sqlite:///test.db")

    def test_defaults_match_requested_chat_and_mention(self):
        with patch.dict("os.environ", {"VK_RELAY_ENABLED": "1", "VK_TG_CHAT_ID": "-100"}, clear=True):
            result = RelaySettings.environment("sqlite:///test.db")
        self.assertEqual(result.peer_id, PEER)
        self.assertEqual(result.mention, "@thelovelesss")


class BrowserLoopTests(TestCase):
    setUp = StoreTests.setUp
    tearDown = StoreTests.tearDown
    def test_browser_responses_drive_delivery_and_save_session(self):
        auth = Path(self.temp.name) / "auth.json"
        auth.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
        settings = RelaySettings(-100, PEER, "@thelovelesss", self.url, str(auth), refresh=0)
        bot = FakeBot()
        relay = VkRelay(bot, settings)
        relay.store = self.store
        relay.delivery.store = self.store

        class Response:
            url = "https://api.vk.ru/method/messages.getHistory"
            headers = {}
            request = SimpleNamespace(resource_type="fetch", post_data=f"peer_id={PEER}")
            def __init__(self, items): self.items = items
            async def body(self):
                return json.dumps({"response": {"items": self.items}}).encode()

        class Page:
            def on(self, event, handler): self.handler = handler
            async def goto(self, *args, **kwargs): self.handler(Response([message(1)]))
            async def reload(self, **kwargs): self.handler(Response([message(1), message(2)]))

        class Context:
            async def route(self, *args): pass
            async def new_page(self): return Page()
            async def storage_state(self, **kwargs): return {"cookies": [], "origins": []}

        class Browser:
            async def new_context(self, **kwargs): return Context()
            async def close(self): pass

        class Chromium:
            async def launch(self, **kwargs): return Browser()

        class Playwright:
            chromium = Chromium()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass

        original = bot.send_message
        def send(chat, text, **kwargs):
            original(chat, text, **kwargs)
            if text == "Message 2":
                relay.stop.set()
        bot.send_message = send
        with self.store.single_worker(), patch("playwright.async_api.async_playwright", return_value=Playwright()):
            asyncio.run(asyncio.wait_for(relay._browse(), timeout=10))
        self.assertIn("Message 2", bot.sent)
        self.assertNotIn("Message 1", bot.sent)
        self.assertEqual(self.store.state()[1], 2)
        self.assertIsNotNone(self.store.load_session())
