"""Read one VK conversation using the web client's cookie authentication.

Protocol reference: t3chn0pr13st/VKBrowserClient, docs/ARCHITECTURE.md.
No developer credentials, browser process or write methods are used here.
"""
from __future__ import annotations

import json
import logging
import time
from copy import deepcopy

import requests

from .messages import extract_messages, message_number


LOGGER = logging.getLogger(__name__)


class VkHttpError(RuntimeError):
    """Only static descriptions/numeric codes may reach logs."""
    auth = False
    review = False


class VkSessionExpired(VkHttpError):
    auth = True


class VkHistoryGap(VkHttpError):
    review = True


class VkHttpClient:
    APP_ID = "6287487"
    API_VERSION = "5.282"
    LOGIN_URL = "https://login.vk.ru/?act=web_token"
    HISTORY_URL = "https://web.api.vk.ru/method/messages.getHistory"

    def __init__(self, state: dict, peer_id: int, *, session=None):
        self.state = deepcopy(state)
        self.peer_id = peer_id
        self.session = session or requests.Session()
        self.session.headers.update({
            "Origin": "https://vk.ru", "Referer": "https://vk.ru/",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0",
        })
        for cookie in state.get("cookies", []):
            domain = str(cookie.get("domain", "")).lower()
            if domain.lstrip(".") not in {"vk.ru", "vk.com", "login.vk.ru", "id.vk.ru"}:
                continue
            expiry = cookie.get("expires", -1)
            if expiry > 0 and expiry <= time.time():
                continue
            self.session.cookies.set(cookie["name"], cookie["value"], domain=domain,
                                     path=cookie.get("path", "/"), secure=cookie.get("secure", True),
                                     expires=int(expiry) if expiry > 0 else None)
        self.token = ""
        self.expires = 0
        self.user_id = int(state.get("vk_http_user_id") or 0)

    def close(self):
        self.session.close()

    def _post(self, url, *, data, params=None):
        try:
            with self.session.post(url, data=data, params=params, timeout=(10, 25),
                                   allow_redirects=False, stream=True) as response:
                if 300 <= response.status_code < 400:
                    raise VkSessionExpired("VK requested a new login")
                if response.status_code == 429:
                    raise VkHttpError("VK HTTP rate limit (429)")
                if not 200 <= response.status_code < 300:
                    raise VkHttpError(f"VK HTTP status {response.status_code}")
                body = bytearray()
                for block in response.iter_content(65536):
                    body.extend(block)
                    if len(body) > 8_000_000:
                        raise VkHttpError("VK response exceeds size limit")
                result = json.loads(body)
        except requests.RequestException:
            raise VkHttpError("VK network request failed") from None
        except (ValueError, UnicodeError):
            raise VkHttpError("VK returned an unexpected response format") from None
        if not isinstance(result, dict):
            raise VkHttpError("VK returned an unexpected response structure")
        return result

    def refresh_token(self):
        result = self._post(self.LOGIN_URL, data={"version": "1", "app_id": self.APP_ID})
        data = result.get("data")
        if result.get("type") != "okay" or not isinstance(data, dict) or not data.get("access_token"):
            raise VkSessionExpired("VK did not issue a web token")
        try:
            user_id = int(data["user_id"])
            expires = int(data["expires"])
        except (KeyError, TypeError, ValueError):
            raise VkSessionExpired("VK token identity or expiry is missing") from None
        if user_id <= 0 or (self.user_id and self.user_id != user_id):
            raise VkSessionExpired("VK account identity changed")
        if expires <= time.time():
            raise VkSessionExpired("VK issued an expired token")
        self.user_id, self.expires, self.token = user_id, expires, str(data["access_token"])
        LOGGER.info("VK HTTP token refreshed")

    def _history_page(self, offset):
        if not self.token or time.time() >= self.expires - 60:
            self.refresh_token()
        for attempt in range(2):
            result = self._post(self.HISTORY_URL,
                                params={"v": self.API_VERSION, "client_id": self.APP_ID},
                                data={"v": self.API_VERSION, "access_token": self.token,
                                      "peer_id": self.peer_id, "count": 100, "offset": offset})
            error = result.get("error")
            if not error:
                payload = result.get("response")
                if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                    raise VkHttpError("VK history response is missing items")
                messages = extract_messages(result, self.peer_id, self.peer_id)
                if payload["items"] and not messages:
                    raise VkHttpError("VK history messages cannot be read safely")
                return messages, len(payload["items"])
            try:
                code = int(error.get("error_code", -1))
            except (AttributeError, TypeError, ValueError):
                code = -1
            if code == 5:
                if attempt == 0:
                    self.refresh_token()
                    continue
                raise VkSessionExpired("VK rejected the refreshed token")
            if code in {14, 17, 18}:
                raise VkSessionExpired(f"VK requires account verification (code {code})")
            raise VkHttpError(f"VK history API error {code}")

    def history_since(self, kind="", cursor=0):
        """Include the latest message even when nothing new arrived (/vk_test).

        Pages overlap to tolerate inserts while fetching. Never advance past an
        unread backlog if the bounded catch-up cannot reach the saved position.
        """
        merged = {}
        for page in range(20):
            messages, count = self._history_page(page * 90)
            for message in messages:
                number_kind, number = message_number(message)
                if kind and number_kind != kind:
                    raise VkHttpError("VK message identifiers changed")
                merged[number] = message
            if not cursor or not messages or count < 100 or min(merged) <= cursor:
                return [merged[number] for number in sorted(merged)]
            time.sleep(0.35)
        raise VkHistoryGap("VK backlog exceeds a single safe catch-up")

    def storage_state(self):
        state = deepcopy(self.state)
        cookies = {(c["domain"], c.get("path", "/"), c["name"]): c for c in state.get("cookies", [])}
        for cookie in self.session.cookies:
            key = cookie.domain, cookie.path, cookie.name
            cookies[key] = {**cookies.get(key, {}), "name": cookie.name, "value": cookie.value,
                            "domain": cookie.domain, "path": cookie.path, "secure": cookie.secure,
                            "expires": cookie.expires if cookie.expires is not None else -1}
        state["cookies"] = list(cookies.values())
        state["vk_http_user_id"] = self.user_id
        return state
