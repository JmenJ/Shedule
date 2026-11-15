import json
import time
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import requests

from vk_relay.http_client import VkHttpClient, VkHttpError, VkSessionExpired, VkHistoryGap
from vk_relay.service import RelaySettings, VkRelay


PEER = 2000000012
STATE = {"cookies": [{"name": "remixsid", "value": "fake-cookie", "domain": ".vk.ru",
                      "path": "/", "secure": True}], "origins": []}


def message(number):
    return {"id": number + 10000, "conversation_message_id": number, "peer_id": PEER,
            "from_id": 123, "date": 1700000000 + number, "text": f"Message {number}"}


def auth(token="fake-token", user=123, expires=None):
    return {"type": "okay", "data": {"access_token": token, "user_id": user,
                                       "expires": expires or int(time.time()) + 900}}


def history(numbers):
    return {"response": {"items": [message(number) for number in numbers]}}


class Response:
    def __init__(self, payload, status=200):
        self.status_code = status
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]


class Session:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.replies)

    def close(self): pass


class HttpClientTests(TestCase):
    def client(self, *responses):
        session = Session([item if isinstance(item, Response) else Response(item) for item in responses])
        return VkHttpClient(STATE, PEER, session=session), session

    def test_expired_api_token_is_refreshed_and_history_retried_once(self):
        client, session = self.client(auth(), {"error": {"error_code": 5}},
                                      auth("new-fake-token"), history([9]))
        self.assertEqual(client.history_since()[0]["conversation_message_id"], 9)
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(session.calls[-1][1]["data"]["access_token"], "new-fake-token")
        self.assertEqual(session.calls[-1][1]["data"]["peer_id"], PEER)

    def test_refresh_before_expiry_and_account_binding(self):
        client, session = self.client(auth(expires=1950), history([9]), auth(user=456, expires=2800))
        with patch("vk_relay.http_client.time.time", return_value=1000):
            client.history_since()
        with patch("vk_relay.http_client.time.time", return_value=1900):
            with self.assertRaises(VkSessionExpired):
                client.history_since()
        self.assertEqual(len(session.calls), 3)

    def test_paging_collects_every_new_message_with_overlapping_pages(self):
        client, _ = self.client(auth(), history(range(301, 201, -1)),
                                history(range(211, 111, -1)), history(range(121, 21, -1)))
        with patch("vk_relay.http_client.time.sleep"):
            messages = client.history_since("conversation_message_id", 100)
        new = [m["conversation_message_id"] for m in messages if m["conversation_message_id"] > 100]
        self.assertEqual(new, list(range(101, 302)))

    def test_unread_backlog_cannot_be_silently_skipped(self):
        client, _ = self.client(auth(), *[history(range(5000, 4900, -1)) for _ in range(20)])
        with patch("vk_relay.http_client.time.sleep"), self.assertRaises(VkHistoryGap):
            client.history_since("conversation_message_id", 1)

    def test_redirects_do_not_send_session_to_another_host(self):
        client, session = self.client(Response({}, status=302))
        with self.assertRaises(VkSessionExpired): client.history_since()
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.calls[0][1]["allow_redirects"])

    def test_error_messages_never_echo_token_or_provider_details(self):
        client, _ = self.client(auth(), {"error": {"error_code": 6, "error_msg": "SECRET-DETAILS",
                                                   "request_params": [{"access_token": "SECRET-TOKEN"}]}})
        with self.assertRaises(VkHttpError) as error: client.history_since()
        self.assertNotIn("SECRET", str(error.exception))

    def test_persistent_auth_rejection_is_not_retried_forever(self):
        client, session = self.client(auth(), {"error": {"error_code": 5}},
                                      auth(), {"error": {"error_code": 5}})
        with self.assertRaises(VkSessionExpired): client.history_since()
        self.assertEqual(len(session.calls), 4)

    def test_cookie_refresh_is_exported_without_token(self):
        client, session = self.client(auth())
        client.refresh_token()
        session.cookies.set("remixsid", "rotated", domain=".vk.ru", path="/")
        state = client.storage_state()
        self.assertEqual(state["cookies"][0]["value"], "rotated")
        self.assertEqual(state["vk_http_user_id"], 123)
        self.assertNotIn("fake-token", json.dumps(state))
        self.assertEqual(STATE["cookies"][0]["value"], "fake-cookie")

    def test_large_and_non_json_responses_are_rejected(self):
        for payload in (b"x" * 8_000_001, b"<html>login</html>"):
            client, _ = self.client(Response(payload))
            with self.assertRaises(VkHttpError): client.history_since()


class HttpWorkerTests(TestCase):
    def relay(self, batches):
        sent = []
        bot = SimpleNamespace(send_message=lambda chat, text, **kwargs: sent.append(text))
        relay = VkRelay(bot, RelaySettings(-100, PEER, "@thelovelesss", "unused", "unused",
                                           storage="memory", transport="http", poll_seconds=0))
        relay.store.save_session(STATE)
        replies = iter(batches)
        client = SimpleNamespace(history_since=lambda *args: next(replies),
                                 storage_state=lambda: STATE, close=lambda: None)
        return relay, client, sent

    def test_no_history_spam_test_copy_does_not_change_checkpoint_and_next_poll_deduplicates(self):
        relay, client, sent = self.relay([[message(1)], [message(1), message(2)], [message(1), message(2)]])
        with patch("vk_relay.http_client.VkHttpClient", return_value=client), relay.store.single_worker():
            relay._http_cycle()
            self.assertEqual(sent, [])
            relay.request_test(-100)
            relay._http_cycle()
            self.assertEqual(sent, ["Message 2", "Message 2"])
            self.assertEqual(relay.store.state()[1], 2)
            relay._http_cycle()
            self.assertEqual(len(sent), 2)

    def test_test_requested_during_read_waits_for_a_fresh_http_query(self):
        relay, client, sent = self.relay([[message(2)]])
        original = client.history_since
        def during_read(*args):
            relay.request_test(-100)
            client.history_since = original
            return [message(1)]
        client.history_since = during_read
        with patch("vk_relay.http_client.VkHttpClient", return_value=client), relay.store.single_worker():
            relay._http_cycle()
            self.assertEqual(sent, [])
            self.assertIsNotNone(relay.test_requested_at)
            relay._http_cycle()
            self.assertEqual(sent, ["Message 2", "Message 2"])
            self.assertIsNone(relay.test_requested_at)

    def test_http_worker_never_starts_a_browser(self):
        relay, client, sent = self.relay([[message(1)], [message(1), message(2)]])
        relay.delivery.bot.send_message = lambda chat, text, **kwargs: (sent.append(text), relay.stop.set())
        with patch("vk_relay.http_client.VkHttpClient", return_value=client), \
             patch.object(relay, "_browse", side_effect=AssertionError("No browser allowed")):
            relay._thread_main()
        self.assertEqual(sent, ["Message 2"])
        self.assertEqual(relay.store.state()[1], 2)
