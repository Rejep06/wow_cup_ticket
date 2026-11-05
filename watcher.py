#!/usr/bin/env python3
"""WOW CUP availability monitor with discovery of the current ticket URL.

Reads public landing-page HTML / Nuxt configuration and the public read-only
Yandex Afisha session endpoint. It does NOT reserve or purchase tickets or
bypass authentication, queues or CAPTCHA.
"""
from __future__ import annotations

import argparse
import ast
import html
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import requests
from dotenv import load_dotenv

load_dotenv()

LOG = logging.getLogger("ticket_watcher")
AFISHA_HOST = "widget.afisha.yandex.ru"
LANDING_URL = os.getenv("LANDING_URL", "https://wowcup.tennis/").strip()
DEFAULT_WIDGET_URL = (
    "https://widget.afisha.yandex.ru/w/sessions/ticketsteam-8261@77813737"
    "?clientKey=5a2f3e1d-f923-4b55-8a34-636ba6dfe922&regionId=213"
)
TICKET_URL = os.getenv("TICKET_URL", "").strip()
OLD_SESSION_KEY = os.getenv("SESSION_KEY", "").strip()
OLD_CLIENT_KEY = os.getenv("CLIENT_KEY", "").strip()

STATE_FILE = Path(os.getenv("STATE_FILE", "data/state.json"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def parse_chat_ids(raw: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(s.strip() for s in raw.split(",") if s.strip()))


TELEGRAM_CHAT_IDS = parse_chat_ids(
    os.getenv("TELEGRAM_CHAT_IDS", "").strip()
    or os.getenv("TELEGRAM_CHAT_ID", "").strip()
)
CHECK_INTERVAL_SECONDS = max(30.0, float(os.getenv("CHECK_INTERVAL_SECONDS", "60")))
MAX_ERROR_INTERVAL_SECONDS = max(
    CHECK_INTERVAL_SECONDS, float(os.getenv("MAX_ERROR_INTERVAL_SECONDS", "3600"))
)
DISCOVERY_INTERVAL_SECONDS = max(60.0, float(os.getenv("DISCOVERY_INTERVAL_SECONDS", "600")))
ALERT_COOLDOWN_SECONDS = max(60.0, float(os.getenv("ALERT_COOLDOWN_SECONDS", "900")))
ERROR_ALERT_THRESHOLD = max(1, int(os.getenv("ERROR_ALERT_THRESHOLD", "3")))
EXPECTED_EVENT_ID = os.getenv("EXPECTED_EVENT_ID", "908192").strip()
EXPECTED_DATE = os.getenv("EXPECTED_DATE", "2026-12-05").strip()
NOTIFY_URL_CHANGES = os.getenv("NOTIFY_URL_CHANGES", "true").lower() not in (
    "0", "no", "false", "off"
)
TIMEOUT = (5, 15)

# The URL comes from the official event site. Do not follow arbitrary hosts.
SESSION_PATH = re.compile(r"^/w/sessions/([A-Za-z0-9_~.@-]{1,256})$")
TICKET_URL_LITERAL = re.compile(
    r"(?:\b|[\"'])ticketUrl[\"']?\s*:\s*(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')",
    re.IGNORECASE,
)


class DiscoveryError(RuntimeError):
    pass


class SessionDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class TicketTarget:
    widget_url: str
    api_url: str
    alias: str


@dataclass(frozen=True)
class Snapshot:
    event: str
    date: str
    count: int
    status: str
    max_tickets: int | None
    session_id: str = ""
    widget_url: str = ""


def _unescape_complete_html_entities(value: str) -> str:
    """Only unescape *terminated* HTML entities.

    html.unescape('...&regionId=213') is unsafe: Python interprets the
    leading '&reg' as the registered-mark entity and destroys the URL.
    """
    return re.sub(
        r"&(?:#[0-9]+|#(?:x|X)[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]+);",
        lambda match: html.unescape(match.group(0)),
        value,
    )


def target_from_url(raw: str) -> TicketTarget:
    """Allow only the official Yandex widget and its public session path."""
    raw = _unescape_complete_html_entities(raw.strip())
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or parsed.hostname != AFISHA_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise DiscoveryError("Ticket URL is not an HTTPS URL on the official widget host")
    match = SESSION_PATH.fullmatch(parsed.path)
    if not match:
        raise DiscoveryError(f"Unexpected Yandex widget path: {parsed.path[:150]}")
    alias = match.group(1)
    query = parse_qs(parsed.query, keep_blank_values=True)
    client_keys = query.get("clientKey", [])
    if len(client_keys) != 1 or not client_keys[0] or len(client_keys[0]) > 150:
        raise DiscoveryError("Missing or invalid clientKey in the ticket URL")
    params = {"clientKey": client_keys[0]}
    if query.get("regionId") and query["regionId"][0]:
        params["regionId"] = query["regionId"][0]
    widget_url = urlunsplit(("https", AFISHA_HOST, parsed.path, urlencode(params), ""))
    api_url = (
        f"https://{AFISHA_HOST}/api/tickets/v1/sessions/{alias}?"
        + urlencode({"clientKey": client_keys[0], "req_number": "0"})
    )
    return TicketTarget(widget_url=widget_url, api_url=api_url, alias=alias)


def configured_fallback() -> TicketTarget:
    # On upgrade, an old SESSION_KEY may still be present in the user's .env.
    # Discovery from the event site always has priority over this fallback.
    if TICKET_URL:
        url = TICKET_URL
    elif OLD_SESSION_KEY and OLD_CLIENT_KEY:
        url = (
            f"https://{AFISHA_HOST}/w/sessions/{OLD_SESSION_KEY}?"
            + urlencode({"clientKey": OLD_CLIENT_KEY})
        )
    else:
        url = DEFAULT_WIDGET_URL
    return target_from_url(url)


def decode_js_literal(literal: str) -> str:
    """Read ticketUrl JS string literals, including escaped slashes/unicode."""
    cleaned = literal.replace(r"\/", "/").replace(r"\:", ":")
    try:
        decoded = json.loads(cleaned) if cleaned[0] == '"' else ast.literal_eval(cleaned)
    except (ValueError, SyntaxError) as exc:
        raise DiscoveryError("Could not decode ticketUrl from Nuxt config") from exc
    if not isinstance(decoded, str):
        raise DiscoveryError("ticketUrl in Nuxt config was not a string")
    return _unescape_complete_html_entities(decoded)


class _TicketLinksParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.script_srcs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.script_srcs.append(values["src"] or "")
        if tag in {"a", "iframe"}:
            link = values.get("href") or values.get("src")
            if link:
                self.links.append(link)


def extract_ticket_url(document: str) -> str | None:
    """Extract an official widget URL from Nuxt config, HTML links, or JS text."""
    for match in TICKET_URL_LITERAL.finditer(document):
        candidate = decode_js_literal(match.group(1))
        # An explicit ticketUrl pointing elsewhere must be reported, not trusted.
        return target_from_url(candidate).widget_url
    parser = _TicketLinksParser()
    parser.feed(document)
    for link in parser.links:
        try:
            return target_from_url(link).widget_url
        except (DiscoveryError, ValueError):
            continue
    return None


def discover_ticket_target(http: Any = requests) -> TicketTarget:
    """Fetch wowcup.tennis, parse inline Nuxt config; inspect same-origin JS if needed."""
    response = http.get(
        LANDING_URL,
        headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0 (TicketStatusWatcher)"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    document = response.text
    found = extract_ticket_url(document)
    if found:
        return target_from_url(found)

    # Nuxt sometimes puts runtime config in a same-origin JS asset instead.
    parser = _TicketLinksParser()
    parser.feed(document)
    origin = urlsplit(LANDING_URL)
    for src in parser.script_srcs[:8]:
        script_url = urljoin(LANDING_URL, src)
        parsed = urlsplit(script_url)
        if parsed.scheme != "https" or parsed.netloc != origin.netloc:
            continue
        try:
            script_response = http.get(
                script_url,
                headers={"Accept": "text/javascript"},
                timeout=TIMEOUT,
            )
            script_response.raise_for_status()
            if len(script_response.content) > 1_000_000:
                continue
            found = extract_ticket_url(script_response.text)
        except (requests.RequestException, DiscoveryError, ValueError) as exc:
            LOG.debug("Cannot read a Nuxt asset: %s", exc)
            continue
        if found:
            return target_from_url(found)
    raise DiscoveryError("The event site did not expose a supported Yandex ticketUrl")


def parse_snapshot(payload: Any, target: TicketTarget) -> Snapshot:
    """Treat unknown formats as errors, never as an empty/sold-out session."""
    if not isinstance(payload, dict):
        raise SessionDataError("API JSON root is not an object")
    if payload.get("status") not in (None, "success"):
        raise SessionDataError(f"API status: {str(payload.get('status'))[:100]}")
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        raise SessionDataError("API result is not an object")
    session = result.get("session")
    if not isinstance(session, dict):
        raise SessionDataError("API contains no result.session object")
    try:
        count = session["availableSeatCount"]
        status = session["saleStatus"]
        date = session["sessionDate"]
        event_data = session["event"]
        event = event_data["name"]
        event_id = event_data["id"]
    except (KeyError, TypeError) as exc:
        raise SessionDataError(f"Missing API field: {exc}") from exc
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise SessionDataError("Invalid availableSeatCount; cannot decide availability")
    if not isinstance(status, str) or not status:
        raise SessionDataError("Invalid session.saleStatus")
    if not isinstance(date, str) or len(date) < 10:
        raise SessionDataError("Invalid sessionDate")
    if not isinstance(event, str) or not event:
        raise SessionDataError("Invalid event name")
    if EXPECTED_EVENT_ID and str(event_id) != EXPECTED_EVENT_ID:
        raise SessionDataError(f"Wrong event ID: {event_id}; expected {EXPECTED_EVENT_ID}")
    if EXPECTED_DATE and date[:10] != EXPECTED_DATE:
        raise SessionDataError(f"Wrong session date: {date}; expected {EXPECTED_DATE}")
    session_id = session.get("id")
    if not isinstance(session_id, int) or isinstance(session_id, bool):
        raise SessionDataError("Invalid session.id")
    max_tickets = session.get("maxTicketsCount")
    if isinstance(max_tickets, bool) or not isinstance(max_tickets, int):
        max_tickets = None
    # session.key can be a canonical Base64 key while the URL contains the
    # ticketsteam-... alias. They must NOT be compared for equality.
    return Snapshot(
        event=event, date=date, count=count, status=status,
        max_tickets=max_tickets, session_id=str(session_id),
        widget_url=target.widget_url,
    )


def fetch_snapshot(http: Any = requests, target: TicketTarget | None = None) -> Snapshot:
    target = target or configured_fallback()
    response = http.get(
        target.api_url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "TicketStatusWatcher/2.0",
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise SessionDataError("API returned non-JSON (HTML, error or challenge)") from exc
    return parse_snapshot(payload, target)


def telegram_call(method: str, payload: dict[str, Any], *, get: bool = False) -> dict:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    if get:
        resp = requests.get(url, params=payload, timeout=TIMEOUT)
    else:
        resp = requests.post(url, data=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    answer = resp.json()
    if not answer.get("ok"):
        raise RuntimeError(f"Telegram error: {answer.get('description')}")
    return answer


class TelegramDeliveryError(RuntimeError):
    def __init__(self, failed_chat_ids: list[str]):
        self.failed_chat_ids = failed_chat_ids
        super().__init__("Could not notify: " + ", ".join(failed_chat_ids))


def send_message(message: str, *, chat_ids: tuple[str, ...] | list[str] | None = None) -> None:
    recipients = tuple(chat_ids) if chat_ids is not None else TELEGRAM_CHAT_IDS
    if not recipients:
        raise RuntimeError("Set TELEGRAM_CHAT_IDS in .env")
    failed: list[str] = []
    for chat_id in recipients:
        try:
            telegram_call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
            )
            LOG.info("Telegram delivered to %s", chat_id)
        except Exception as exc:
            LOG.warning("Telegram failed for %s: %s", chat_id, exc)
            failed.append(chat_id)
    if failed:
        raise TelegramDeliveryError(failed)


def ticket_message(snapshot: Snapshot, reason: str) -> str:
    heading = (
        "🎟 <b>Свободных билетов стало больше!</b>"
        if reason == "increased" else "🎟 <b>Билеты появились!</b>"
    )
    limit = f"\nМаксимум в заказе: {snapshot.max_tickets}" if snapshot.max_tickets else ""
    return (
        f"{heading}\n"
        f"{html.escape(snapshot.event)}\n"
        f"Дата: {html.escape(snapshot.date)}\n"
        f"Свободно: <b>{snapshot.count}</b>\n"
        f"Статус: {html.escape(snapshot.status)}{limit}\n\n"
        f'<a href="{html.escape(snapshot.widget_url, quote=True)}">Открыть билеты</a>\n'
        "Наличие может измениться до покупки."
    )


def load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError):
        LOG.warning("Bad state JSON; starting from empty state")
        return {}


def save_state(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def active_target(state: dict[str, Any]) -> TicketTarget:
    if state.get("ticket_url"):
        try:
            return target_from_url(str(state["ticket_url"]))
        except (DiscoveryError, ValueError):
            LOG.warning("Cached ticket URL invalid; reverting to configured fallback")
    return configured_fallback()


def set_target(state: dict[str, Any], target: TicketTarget) -> bool:
    old = state.get("ticket_url")
    changed = bool(old and old != target.widget_url)
    state["ticket_url"] = target.widget_url
    if changed:
        LOG.warning("Official ticket URL changed: %s -> %s", old, target.widget_url)
        state["count"] = None
        state["session_id"] = None
        state["pending_ticket_alert"] = False
        state["pending_ticket_chat_ids"] = []
        if NOTIFY_URL_CHANGES:
            state["pending_url_notification"] = target.widget_url
            state["pending_url_chat_ids"] = list(TELEGRAM_CHAT_IDS)
    return changed


def refresh_target(state: dict[str, Any], http: Any, now: float, *, force: bool = False) -> TicketTarget:
    """Try website discovery on schedule; use cached URL during a site outage."""
    current = active_target(state)
    last = float(state.get("last_discovery_attempt_at", 0))
    if not force and last and now - last < DISCOVERY_INTERVAL_SECONDS:
        return current
    state["last_discovery_attempt_at"] = now
    try:
        found = discover_ticket_target(http)
    except (requests.RequestException, DiscoveryError, ValueError) as exc:
        state["last_discovery_error"] = str(exc)[:300]
        LOG.warning("Website URL discovery failed; keep current target: %s", exc)
        if not state.get("ticket_url"):
            state["ticket_url"] = current.widget_url
        return current
    state["last_discovery_error"] = None
    state["last_discovery_success_at"] = now
    set_target(state, found)
    return found


def notify_url_change(state: dict[str, Any]) -> None:
    """URL changes are informational, NOT proof that tickets are on sale."""
    url = state.get("pending_url_notification")
    if not url:
        return
    recipients = state.get("pending_url_chat_ids") or list(TELEGRAM_CHAT_IDS)
    message = (
        "🔗 <b>Ссылка на продажу WOW CUP изменилась!</b>\n"
        "Нашёл новую ссылку на официальном сайте. Наличие билетов пока не подтверждено.\n\n"
        f'<a href="{html.escape(url, quote=True)}">Открыть новую ссылку</a>'
    )
    try:
        send_message(message, chat_ids=recipients)
    except TelegramDeliveryError as exc:
        state["pending_url_chat_ids"] = exc.failed_chat_ids
    except Exception as exc:
        LOG.warning("Could not deliver new URL notice: %s", exc)
    else:
        state["pending_url_notification"] = None
        state["pending_url_chat_ids"] = []


def alert_reason(state: dict[str, Any], snapshot: Snapshot, now: float) -> str | None:
    if snapshot.count <= 0:
        return None
    if state.get("pending_ticket_alert"):
        return "retry"
    old = state.get("count")
    if old is None or old == 0:
        return "appeared"
    last = float(state.get("last_ticket_alert_at", 0))
    if snapshot.count > old and now - last >= ALERT_COOLDOWN_SECONDS:
        return "increased"
    return None


def process_snapshot(snapshot: Snapshot, state: dict[str, Any], now: float) -> None:
    if state.get("session_id") not in (None, "", snapshot.session_id):
        LOG.warning("Session ID changed: %s -> %s", state["session_id"], snapshot.session_id)
        state["count"] = None
        state["pending_ticket_alert"] = False
        state["pending_ticket_chat_ids"] = []
    reason = alert_reason(state, snapshot, now)
    if reason:
        retry_ids = state.get("pending_ticket_chat_ids") if reason == "retry" else None
        try:
            send_message(ticket_message(snapshot, reason), chat_ids=retry_ids)
        except TelegramDeliveryError as exc:
            state["pending_ticket_alert"] = True
            state["pending_ticket_chat_ids"] = exc.failed_chat_ids
        except Exception as exc:
            LOG.warning("Ticket alert delayed: %s", exc)
            state["pending_ticket_alert"] = True
        else:
            state["last_ticket_alert_at"] = now
            state["pending_ticket_alert"] = False
            state["pending_ticket_chat_ids"] = []
    elif snapshot.count == 0:
        state["pending_ticket_alert"] = False
        state["pending_ticket_chat_ids"] = []

    if int(state.get("consecutive_errors", 0)) >= ERROR_ALERT_THRESHOLD:
        try:
            send_message("✅ <b>Мониторинг Яндекс Афиши снова работает.</b>")
        except Exception as exc:
            LOG.warning("Could not send recovery message: %s", exc)
    state.update(
        count=snapshot.count,
        session_id=snapshot.session_id,
        status=snapshot.status,
        session_date=snapshot.date,
        last_checked_at=datetime.now().astimezone().isoformat(),
        consecutive_errors=0,
    )
    LOG.info("Session %s: available=%s, saleStatus=%s", snapshot.session_id, snapshot.count, snapshot.status)


def process_failure(exc: Exception, state: dict[str, Any], now: float) -> None:
    failures = int(state.get("consecutive_errors", 0)) + 1
    state["consecutive_errors"] = failures
    state["last_api_error"] = str(exc)[:300]
    LOG.warning("API check failed #%d: %s", failures, exc)
    last = float(state.get("last_error_alert_at", 0))
    if failures >= ERROR_ALERT_THRESHOLD and now - last >= 3600:
        try:
            send_message(
                "⚠️ <b>Мониторинг билетов временно не работает.</b>\n"
                f"Ошибок подряд: {failures}.\n"
                "Официальный API недоступен или изменился. Бот продолжит попытки."
            )
            state["last_error_alert_at"] = now
        except Exception as telegram_exc:
            LOG.warning("Could not send API error notification: %s", telegram_exc)


def next_check_delay(consecutive_errors: int) -> float:
    if consecutive_errors <= 0:
        return CHECK_INTERVAL_SECONDS
    return min(
        MAX_ERROR_INTERVAL_SECONDS,
        CHECK_INTERVAL_SECONDS * (2 ** min(consecutive_errors, 32)),
    )


def server_retry_after(exc: Exception) -> float:
    """Respect Retry-After (particularly on 429). Server may require >1h."""
    if not isinstance(exc, requests.HTTPError) or exc.response is None:
        return 0.0
    header = exc.response.headers.get("Retry-After", "").strip()
    if not header:
        return 0.0
    try:
        return max(0.0, float(header))
    except ValueError:
        try:
            dt = parsedate_to_datetime(header)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


def should_refresh_after_failure(exc: Exception) -> bool:
    # 429 means rate-limited: respect backoff, do not retry immediately.
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code != 429
    # Also refresh on 200 with an unexpected JSON schema, and on 404 etc.
    return True


def poll(state: dict[str, Any], http: Any, now: float) -> Snapshot:
    """Check current session; if it failed, re-read the site and retry only if URL changed."""
    target = refresh_target(state, http, now)
    notify_url_change(state)
    try:
        return fetch_snapshot(http, target)
    except (requests.RequestException, SessionDataError, DiscoveryError) as exc:
        if not should_refresh_after_failure(exc):
            raise
        refreshed = refresh_target(state, http, now, force=True)
        notify_url_change(state)
        if refreshed.widget_url == target.widget_url:
            raise
        LOG.info("Retrying with new official ticket URL: %s", refreshed.widget_url)
        return fetch_snapshot(http, refreshed)


def monitor() -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        raise RuntimeError("Fill TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS in .env")
    state = load_state()
    LOG.info("Monitoring event site %s", LANDING_URL)
    try:
        send_message("👀 <b>Мониторинг WOW CUP запущен.</b> Проверяю билеты автоматически.")
    except Exception as exc:
        LOG.warning("Startup notification error: %s", exc)

    with requests.Session() as http:
        while True:
            now = time.time()
            last_error: Exception | None = None
            try:
                snapshot = poll(state, http, now)
            except (requests.RequestException, SessionDataError, DiscoveryError, ValueError) as exc:
                last_error = exc
                process_failure(exc, state, now)
            else:
                process_snapshot(snapshot, state, now)
            save_state(state)
            delay = next_check_delay(int(state.get("consecutive_errors", 0)))
            if last_error is not None:
                delay = max(delay, server_retry_after(last_error))
            LOG.info("Next check in %.0f seconds", delay)
            time.sleep(delay)


def print_chat_ids() -> None:
    data = telegram_call("getUpdates", {"limit": 100}, get=True)
    chats: dict[int, str] = {}
    for item in data.get("result", []):
        message = item.get("message") or item.get("channel_post") or {}
        chat = message.get("chat") or {}
        if "id" in chat:
            chats[chat["id"]] = chat.get("title") or chat.get("first_name") or ""
    if not chats:
        print("No chat IDs. Send /start to the bot, then repeat --chat-id.")
    else:
        for chat_id, name in chats.items():
            print(chat_id, name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor WOW CUP tickets and alert on Telegram")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Discover URL and fetch one snapshot")
    mode.add_argument("--discover", action="store_true", help="Show URL found on wowcup.tennis")
    mode.add_argument("--test", action="store_true", help="Test Telegram notification to all chats")
    mode.add_argument("--chat-id", action="store_true", help="Read chat IDs from Telegram updates")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.discover:
            with requests.Session() as http:
                target = discover_ticket_target(http)
            print(f"Widget: {target.widget_url}\nAPI: {target.api_url}")
        elif args.once:
            with requests.Session() as http:
                state = load_state()
                target = refresh_target(state, http, time.time(), force=True)
                snapshot = fetch_snapshot(http, target)
            print(
                f"Event: {snapshot.event}\nDate: {snapshot.date}\n"
                f"Session ID: {snapshot.session_id}\nAvailable: {snapshot.count}\n"
                f"Status: {snapshot.status}\nWidget: {snapshot.widget_url}"
            )
        elif args.test:
            send_message("✅ <b>Тест успешен!</b> Уведомления о WOW CUP работают.")
            print("Test notification sent to configured chats.")
        elif args.chat_id:
            print_chat_ids()
        else:
            monitor()
    except KeyboardInterrupt:
        print("Stopped.")
    except Exception as exc:
        LOG.error("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
