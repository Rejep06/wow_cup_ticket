"""Configuration, one-process lock, paced read-only requests and independent notification delivery."""
from __future__ import annotations
import asyncio
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import httpx
from core import (ApiError, ProtocolError, Target, DEFAULT_TICKET_URL, WIDGET_HOST,
                  allowed_cdn_url, is_challenge, retry_after_seconds, rub_to_minor, unwrap_hallplan)

LOG = logging.getLogger('tickets')


def flag(key: str, default: bool = False) -> bool:
    v = os.getenv(key, str(default)).lower().strip()
    if v not in ('true', 'false', '1', '0'):
        raise ValueError(f'{key}: используйте true / false.')
    return v in ('true', '1')


def number(key: str, default: float, minimum: float) -> float:
    value = float(os.getenv(key, str(default)))
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f'{key} должно быть не меньше {minimum}.')
    return value


@dataclass
class Config:
    root: Path
    target: Target
    event_id: int
    date: str
    interval: float
    jitter: float
    headless: bool
    auto_booking: bool
    auto_sbp: bool
    quantity: int
    min_quantity: int
    max_price: int
    max_total: int
    adjacent: bool
    sectors: tuple[str, ...]
    contacts: dict[str, str]
    token: str
    chats: tuple[str, ...]
    owner: str
    discovery_interval: float
    timeout: float

    @classmethod
    def load(cls, root: Path) -> 'Config':
        chats = tuple(dict.fromkeys(x.strip() for x in os.getenv('TELEGRAM_CHAT_IDS', os.getenv('TELEGRAM_CHAT_ID', '')).split(',') if x.strip()))
        owner = os.getenv('BOOKING_OWNER_CHAT_ID', '').strip() or (chats[0] if len(chats) == 1 else '')
        if any(not re.fullmatch(r'-?\d+', x) for x in (*chats, *([owner] if owner else []))):
            raise ValueError('chat_id должен быть целым числом.')
        quantity = int(os.getenv('BOOKING_SEATS', '1'))
        if quantity not in (1, 2):
            raise ValueError('BOOKING_SEATS: 1 или 2; лимит сеанса проверяется отдельно.')
        min_quantity = int(os.getenv('BOOKING_MIN_SEATS', str(quantity)))
        if min_quantity not in (1, 2) or min_quantity > quantity:
            raise ValueError('BOOKING_MIN_SEATS: 1 <= минимум <= BOOKING_SEATS <= 2.')
        jitter = number('POLL_JITTER_FRACTION', .1, 0)
        if jitter > .5:
            raise ValueError('POLL_JITTER_FRACTION: от 0 до 0.5.')
        date = os.getenv('EXPECTED_DATE', '2026-12-05')
        from datetime import date as Date
        Date.fromisoformat(date)
        raw_price, raw_total = os.getenv('BOOKING_MAX_PRICE_RUB', ''), os.getenv('BOOKING_MAX_TOTAL_RUB', '')
        return cls(root, Target.parse(os.getenv('TICKET_URL', DEFAULT_TICKET_URL)),
                   int(os.getenv('EXPECTED_EVENT_ID', '908192')), date,
                   number('CHECK_INTERVAL_SECONDS', 10, 10), jitter,
                   flag('BOOKING_HEADLESS'), flag('AUTO_BOOKING'), flag('AUTO_START_SBP'),
                   quantity, min_quantity, rub_to_minor(raw_price) if raw_price else 0,
                   rub_to_minor(raw_total) if raw_total else 0, flag('BOOKING_ADJACENT', True),
                   tuple(x.strip() for x in os.getenv('BOOKING_SECTORS', '').split(',') if x.strip()),
                   {'name': os.getenv('BUYER_FULL_NAME', '').strip(),
                    'email': os.getenv('BUYER_EMAIL', '').strip(), 'phone': os.getenv('BUYER_PHONE', '').strip()},
                   os.getenv('TELEGRAM_BOT_TOKEN', '').strip(), chats, owner,
                   number('DISCOVERY_INTERVAL_SECONDS', 600, 300), number('REQUEST_TIMEOUT_SECONDS', 15, 2))

    def validate_automation(self) -> None:
        if not self.auto_booking:
            return
        if self.headless:
            raise ValueError('Для AUTO_BOOKING нужен BOOKING_HEADLESS=false: CAPTCHA/оплата выполняются человеком.')
        if not self.max_price or not self.max_total:
            raise ValueError('Перед AUTO_BOOKING задайте оба лимита цены в .env.')
        if not all(self.contacts.values()):
            raise ValueError('Для AUTO_BOOKING заполните BUYER_FULL_NAME, BUYER_EMAIL, BUYER_PHONE локально.')
        if self.token and not self.owner:
            raise ValueError('При нескольких chat_id укажите BOOKING_OWNER_CHAT_ID для статуса заказа.')


class SingleInstance:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open('a+b')
        self.handle.seek(0)
        if path.stat().st_size == 0:
            self.handle.write(b'0')
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError('Уже запущен экземпляр с этой папкой data; второй запуск запрещён.') from None

    def close(self) -> None:
        self.handle.close()


class ReadAPI:
    def __init__(self, context: Any, timeout: float):
        self.context, self.timeout = context, timeout
        self.count = 0

    async def json(self, url: str) -> dict:
        p = urlsplit(url)
        if not (p.scheme == 'https' and p.hostname == WIDGET_HOST and
                p.port in (None, 443) and not p.username and not p.password and
                re.fullmatch(r'/api/tickets/v[14]/sessions/[^/]+(?:/hallplan)?', p.path)) and not allowed_cdn_url(url):
            raise ProtocolError('GET вне разрешённого списка маршрутов.')
        self.count += 1
        response = None
        try:
            response = await self.context.request.get(url, timeout=self.timeout * 1000,
                max_redirects=0, headers={'Accept': 'application/json', 'Cache-Control': 'no-cache'})
            status = response.status
            wait = retry_after_seconds(response.headers.get('retry-after'))
            if not 200 <= status < 300:
                raise ApiError(f'HTTP {status}', status, wait)
            body = await response.body()
            if len(body) > 8_000_000:
                raise ProtocolError('Слишком большой ответ JSON.')
            try:
                data = json.loads(body)
            except (ValueError, UnicodeError):
                raise ApiError('Ответ не JSON: проверка защиты или изменённый протокол.', status) from None
            if is_challenge(data):
                raise ApiError('CAPTCHA_REQUIRED', 403, wait)
            if not isinstance(data, dict):
                raise ProtocolError('API вернул не объект JSON.')
            return data
        except (ApiError, ProtocolError):
            raise
        except Exception as exc:
            raise ApiError(f'Ошибка чтения API: {type(exc).__name__}') from None
        finally:
            if response is not None:
                await response.dispose()

    async def hallplan(self, target: Target) -> dict:
        # Never continuously poll an old CDN hash. Ask v4 for the current pointer.
        for attempt in range(3):
            data = await self.json(target.api_url(4, '/hallplan'))
            kind, result = unwrap_hallplan(data)
            if kind == 'cdn':
                kind, result = unwrap_hallplan(await self.json(result))
            if kind == 'ready':
                return result
            if kind != 'pending':
                raise ProtocolError('Цепочка hallplan не закончилась данными.')
            if attempt < 2:
                await asyncio.sleep(2)  # Poll a single async read operation, not a 1-Hz inventory loop.
        raise ProtocolError('hallplan всё ещё формируется; следующий обычный цикл.')


class Notifier:
    """Persistent per-recipient outbox. Slow Telegram never blocks checkout.
    No private payment URLs, account IDs, contacts or tokens are placed in messages.
    Network timeouts may produce duplicates (Telegram has no sendMessage idempotency key).
    """
    def __init__(self, path: Path, token: str, chats: tuple[str, ...], owner: str):
        self.path, self.token, self.chats, self.owner = path, token, chats, owner
        self.items = []
        if path.exists():
            try:
                items = json.loads(path.read_text())
                if isinstance(items, list):
                    self.items = [x for x in items if isinstance(x, dict) and x.get('expires', 0) > time.time()]
            except (OSError, ValueError):
                LOG.warning('Очередь Telegram не прочитана; это не разрешает повторную бронь.')
        self.wake = asyncio.Event()
        self.task = None
        self.client = httpx.AsyncClient(timeout=8, follow_redirects=False)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.items, ensure_ascii=False), encoding='utf-8')
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    def enqueue(self, key: str, text: str, private: bool = False) -> None:
        LOG.info('%s', text.replace('\n', ' | '))
        recipients = (self.owner,) if private and self.owner else (() if private else self.chats)
        if not self.token or not recipients:
            return
        existing = {x['key'] for x in self.items}
        if key in existing:
            return
        self.items.append({'key': key, 'text': text, 'pending': list(recipients),
                           'expires': time.time() + 180, 'due': 0, 'failures': 0})
        self.save()
        self.wake.set()

    def start(self) -> None:
        self.task = asyncio.create_task(self.run())

    async def run(self) -> None:
        while True:
            now = time.time()
            self.items = [x for x in self.items if x['expires'] > now and x['pending']]
            for item in list(self.items):
                if item['due'] > now:
                    continue
                retry = 0
                for chat in list(item['pending']):
                    try:
                        response = await self.client.post(f'https://api.telegram.org/bot{self.token}/sendMessage',
                            json={'chat_id': chat, 'text': item['text'], 'disable_web_page_preview': True})
                        body = response.json()
                        if response.is_success and body.get('ok') is True:
                            item['pending'].remove(chat)
                            self.save()
                        else:
                            if response.status_code in (400, 403):
                                LOG.warning('Telegram: получатель недоступен; проверьте /start и chat_id.')
                                item['pending'].remove(chat)
                            retry = max(retry, float(body.get('parameters', {}).get('retry_after', 0)))
                    except Exception:
                        LOG.warning('Telegram: ошибка доставки; токен и адрес запроса не журналируются.')
                    await asyncio.sleep(.1)
                item['failures'] += 1
                item['due'] = time.time() + max(retry, min(60, 2 ** min(item['failures'], 6)))
            self.save()
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        await self.client.aclose()
