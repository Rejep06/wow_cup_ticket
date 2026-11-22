"""Pure, tested rules for the checkout assistant. No network or purchase calls."""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit, quote, unquote

WIDGET_HOST = 'widget.afisha.yandex.ru'
WIDGET_ORIGIN = 'https://' + WIDGET_HOST
DEFAULT_TICKET_URL = (WIDGET_ORIGIN + '/w/sessions/'
    'MTEwOTYzfDkwODE5MnwxNDk2OTE2MnwxNzk2NDgxMDAwMDAw'
    '?clientKey=5a2f3e1d-f923-4b55-8a34-636ba6dfe922&regionId=213')


class ProtocolError(Exception):
    """Unknown response is not interpreted as sold out or reserved."""


class ApiError(ProtocolError):
    def __init__(self, message: str, status: int = 0, retry_after: float = 0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def strict_int(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ProtocolError(f'Invalid integer: {name}')
    return value


def rub_to_minor(value: str) -> int:
    try:
        amount = Decimal(value.strip().replace(',', '.'))
        if not amount.is_finite() or amount <= 0 or amount * 100 != (amount * 100).to_integral_value():
            raise ValueError
        return int(amount * 100)
    except (InvalidOperation, ValueError):
        raise ValueError('Укажите положительную сумму в рублях, максимум 2 знака после запятой.') from None


def decode_html_entities(text: str) -> str:
    # html.unescape on raw query strings converts &regionId to ®ionId (legacy &reg).
    # Decode only explicitly terminated HTML entities, exactly once per occurrence.
    return re.sub(r'&(?:#[0-9]+|#x[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);',
                  lambda m: html.unescape(m.group(0)), text)


@dataclass(frozen=True)
class Target:
    key: str
    client_key: str
    region_id: str = '213'

    @classmethod
    def parse(cls, url: str) -> 'Target':
        parts = urlsplit(decode_html_entities(url.strip()).replace('\\/', '/'))
        if (parts.scheme != 'https' or parts.hostname != WIDGET_HOST or
                parts.port not in (None, 443) or parts.username or parts.password or parts.fragment):
            raise ProtocolError('Неподдерживаемый адрес виджета.')
        match = re.fullmatch(r'/w/sessions/([^/]+)', parts.path)
        query = parse_qs(parts.query)
        if not match or len(query.get('clientKey', [])) != 1:
            raise ProtocolError('В ссылке нет session key / clientKey.')
        key = unquote(match[1])
        if not re.fullmatch(r'[A-Za-z0-9_@=+\-]+', key):
            raise ProtocolError('Неожиданный формат ключа сеанса.')
        return cls(key, query['clientKey'][0], query.get('regionId', ['213'])[0])

    @property
    def url(self) -> str:
        return (WIDGET_ORIGIN + '/w/sessions/' + quote(self.key, safe='@') + '?' +
                urlencode({'clientKey': self.client_key, 'regionId': self.region_id}))

    def api_url(self, version: int = 1, suffix: str = '') -> str:
        return (f'{WIDGET_ORIGIN}/api/tickets/v{version}/sessions/' +
                quote(self.key, safe='@') + suffix + '?' +
                urlencode({'clientKey': self.client_key}))

    def selected_url(self, seats: list['Seat']) -> str:
        selection = [{'level': s.level_id, 'row': s.row, 'place': s.place} for s in seats]
        return self.url + '&' + urlencode({'selectedSeats': json.dumps(selection, ensure_ascii=False, separators=(',', ':'))})


def discover_ticket_url(text: str) -> str:
    """Parse data, never execute website JavaScript. Fail on ambiguous candidates."""
    text = decode_html_entities(text).replace('\\/', '/').replace('\\u0026', '&').replace('\\u003A', ':')
    configured = re.findall(r'''["']?ticketUrl["']?\s*:\s*["']([^"']+)["']''', text)
    links = configured or re.findall(r'''https://widget\.afisha\.yandex\.ru/w/sessions/[^\s"'<>]+''', text)
    candidates: dict[str, str] = {}
    for url in links:
        try:
            target = Target.parse(url)
            candidates[target.url] = target.url
        except (ValueError, ProtocolError):
            pass
    if len(candidates) != 1:
        raise ProtocolError(f'На странице найдено ссылок-кандидатов: {len(candidates)}; адрес не изменён.')
    return next(iter(candidates))


def allowed_cdn_url(url: str) -> bool:
    try:
        p = urlsplit(url)
        return bool(p.scheme == 'https' and not p.username and not p.password and
                    p.port in (None, 443) and p.hostname and
                    p.hostname.endswith('.svc.cdn.yandex.net') and
                    re.fullmatch(r'/hallplans/[a-fA-F0-9]{32,128}', p.path) and not p.fragment)
    except ValueError:
        return False


def is_challenge(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    return (data.get('captchaRequired') is True or
            data.get('authorizationStatus') == 'captcha_required' or
            any(is_challenge(data[k]) for k in ('result', 'error') if k in data))


def validate_session(data: Any, event_id: int, date: str) -> dict:
    if is_challenge(data):
        raise ApiError('CAPTCHA_REQUIRED', 403)
    if not isinstance(data, dict) or data.get('status') != 'success':
        raise ProtocolError('Сеанс: нет status=success.')
    result = data.get('result')
    if not isinstance(result, dict):
        raise ProtocolError('Нет объекта result.')
    session = result.get('session')
    if not isinstance(session, dict):
        raise ProtocolError('Нет result.session.')
    if not isinstance(session.get('event'), dict) or session['event'].get('id') != event_id:
        raise ProtocolError('Изменился event.id — оформление остановлено.')
    if not str(session.get('sessionDate', '')).startswith(date + 'T'):
        raise ProtocolError('Изменилась дата — оформление остановлено.')
    strict_int(session.get('availableSeatCount'), 'availableSeatCount')
    strict_int(session.get('maxTicketsCount'), 'maxTicketsCount', 1)
    key = session.get('key')
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_@=+\-]+', key):
        raise ProtocolError('Сеанс без корректного key.')
    if session.get('saleStatus') not in ('available', 'no-seats', 'no-sessions', 'closed', 'not-started'):
        raise ProtocolError('Неизвестный saleStatus сеанса.')
    return session


def unwrap_hallplan(data: Any) -> tuple[str, Any]:
    """Return ('ready', hallplan), ('cdn', URL), or ('pending', None)."""
    if is_challenge(data):
        raise ApiError('CAPTCHA_REQUIRED', 403)
    if not isinstance(data, dict):
        raise ProtocolError('Схема не является объектом JSON.')
    if data.get('status') == 'completed' and isinstance(data.get('url'), str):
        if not allowed_cdn_url(data['url']):
            raise ProtocolError('Неподдерживаемый CDN URL; переход отменён.')
        return 'cdn', data['url']
    result = data.get('result')
    if isinstance(result, dict) and data.get('status') == 'success':
        plan = result.get('hallplan')
        if isinstance(plan, dict) and result.get('operationStatus') in (None, 'completed'):
            if result.get('saleStatus') not in ('available', 'no-seats'):
                raise ProtocolError('Неизвестный saleStatus схемы.')
            if not isinstance(plan.get('levels'), list):
                raise ProtocolError('Нет hallplan.levels.')
            count = strict_int(plan.get('availableSeatCount'), 'hallplan.availableSeatCount')
            if result.get('saleStatus') == 'no-seats' and count > 0:
                raise ProtocolError('Противоречивый счётчик схемы.')
            return 'ready', plan
        if result.get('operationStatus') in ('running', 'pending', 'processing'):
            return 'pending', None
    if data.get('status') in ('running', 'pending', 'processing'):
        return 'pending', None
    raise ProtocolError('Неизвестный ответ hallplan; не считаем его отсутствием билетов.')


@dataclass(frozen=True)
class Seat:
    level_id: int
    level: str
    row: str
    place: str
    source_id: str
    service_id: str
    gateway_id: str
    ext_session_id: int
    price: int
    total: int
    x: float | None = None
    y: float | None = None

    @property
    def identity(self) -> str:
        return '|'.join((self.service_id, self.gateway_id, str(self.ext_session_id),
                         str(self.level_id), self.source_id))


def money_value(data: Any) -> int:
    if not isinstance(data, dict) or str(data.get('currencyCode', '')).lower() != 'rub':
        raise ProtocolError('Цена должна иметь валюту RUB.')
    return strict_int(data.get('value'), 'money.value')


def extract_seats(plan: dict) -> list[Seat]:
    """Only dynamic hallplan entries are eligible, never static geometry."""
    output, ids = [], set()
    if not isinstance(plan.get('levels'), list):
        raise ProtocolError('Нет списка levels.')
    strict_int(plan.get('availableSeatCount'), 'availableSeatCount')
    for level in plan.get('levels', []):
        if not isinstance(level, dict):
            raise ProtocolError('Некорректный уровень схемы.')
        if level.get('admission') is True:
            continue  # Separate adapter needed for general admission.
        if not isinstance(level.get('seats', []), list):
            raise ProtocolError('Некорректный список seats.')
        for raw in level.get('seats', []):
            try:
                if not isinstance(raw, dict) or not isinstance(raw.get('seat'), dict):
                    raise ProtocolError('Нет объекта seat.')
                info = raw['seat']
                if raw.get('admission') or info.get('linkedSeats'):
                    continue  # Do not split compulsory linked bundles.
                sid, service, gateway = raw['sourceSeatId'], raw['serviceId'], raw['gatewaySessionId']
                if not all(isinstance(v, str) and v for v in (sid, service, gateway)):
                    raise ProtocolError('Нет идентификаторов поставщика места.')
                price, total = money_value(raw['priceInfo']['price']), money_value(raw['priceInfo']['total'])
                if total < price:
                    raise ProtocolError('Итоговая цена меньше цены билета.')
                level_id = strict_int(raw.get('levelId', level['id']), 'levelId')
                if level_id != level['id']:
                    raise ProtocolError('Несовпадение levelId.')
                x, y = info.get('xCoord'), info.get('yCoord')
                if not all(type(v) in (int, float) and math.isfinite(v) for v in (x, y)):
                    x = y = None
                seat = Seat(level_id, str(level['name']), str(info['row']), str(info['place']),
                            sid, service, gateway, strict_int(raw['extSessionId'], 'extSessionId', 1),
                            price, total, x, y)
            except (KeyError, TypeError, ValueError) as exc:
                raise ProtocolError('Изменилась структура места; автоматический выбор отменён.') from exc
            if seat.identity not in ids:
                output.append(seat)
                ids.add(seat.identity)
    return output


def choose_seats(seats: list[Seat], quantity: int, per_ticket: int, total_limit: int,
                 max_allowed: int = 2, sectors: tuple[str, ...] = (), adjacent: bool = True,
                 max_gap: float = 35, min_quantity: int | None = None) -> list[Seat]:
    """Select as MANY as possible, then the least expensive set.

    quantity is the maximum. When min_quantity is omitted, preserve the older
    exact-quantity behavior (used by previous clients and tests). Set
    min_quantity=1 to prefer two affordable tickets but fall back to one.

    Different sectors/rows are permitted for adjacent=False, but all seats in
    a single order must share the same supplier/session. Without that guard we
    could create an invalid mixed-provider order (allowCompositeOrders=false).
    No API mutations take place here.
    """
    if min_quantity is None:
        min_quantity = quantity
    if (type(quantity) is not int or type(min_quantity) is not int or
            quantity not in (1, 2) or not 1 <= min_quantity <= quantity or
            type(max_allowed) is not int or max_allowed < min_quantity or
            per_ticket <= 0 or total_limit <= 0):
        return []
    effective_max = min(quantity, max_allowed)
    normalized_sectors = {name.casefold() for name in sectors}
    pool = [s for s in seats if s.total > 0 and s.total <= per_ticket and
            (not normalized_sectors or s.level.casefold() in normalized_sectors)]
    pool.sort(key=lambda s: (s.total, s.level, s.row, s.place, s.identity))
    if not pool:
        return []

    if effective_max == 2:
        # Group and sort once. Non-adjacent selection runs in O(n log n)
        # instead of checking every possible pair across a large hallplan.
        groups: dict[tuple[str, str, int], list[Seat]] = {}
        for s in pool:
            groups.setdefault((s.service_id, s.gateway_id, s.ext_session_id), []).append(s)
        candidates: list[list[Seat]] = []
        if not adjacent:
            for group in groups.values():
                if len(group) >= 2 and group[0].total + group[1].total <= total_limit:
                    candidates.append(group[:2])
        else:
            for group in groups.values():
                by_row: dict[tuple[int, str], dict[int, list[Seat]]] = {}
                for seat in group:
                    if seat.place.isdecimal() and seat.x is not None and seat.y is not None:
                        by_row.setdefault((seat.level_id, seat.row), {}).setdefault(int(seat.place), []).append(seat)
                for indexed in by_row.values():
                    for place, left_seats in indexed.items():
                        for left in left_seats:
                            for right in indexed.get(place + 1, []):
                                if left.identity != right.identity and left.total + right.total <= total_limit and \
                                        math.hypot(left.x - right.x, left.y - right.y) <= max_gap:
                                    candidates.append([left, right])
        if candidates:
            return min(candidates, key=lambda pair: (sum(s.total for s in pair),
                                                    tuple(s.identity for s in pair)))

    # The owner requested up to two seats: if no affordable, compatible pair
    # exists, accept exactly one seat (but never if min_quantity == 2).
    if min_quantity == 1 and pool[0].total <= total_limit:
        return [pool[0]]
    return []


def retry_after_seconds(value: str | None, now: float | None = None) -> float:
    now = time.time() if now is None else now
    if not value:
        return 0
    try:
        seconds = float(value)
        if math.isfinite(seconds):
            return max(0.0, seconds)
        return 0
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, date.timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return 0


def failure_delay(errors: int, base: float = 60, cap: float = 3600,
                  retry_after: float = 0, rate_limited: bool = False) -> float:
    backoff = min(cap, base * (2 ** min(max(errors, 0), 16)))
    if rate_limited:
        backoff = max(300, backoff)
    return max(backoff, retry_after)  # Retry-After may exceed our 1-hour local cap.


def audit_shape(value: Any, depth: int = 0) -> Any:
    """Preserve JSON keys and types, not personal data, IDs, URLs or tokens."""
    if depth > 10:
        return '<depth-limit>'
    if isinstance(value, dict):
        result = {}
        for k, v in list(value.items())[:80]:
            if re.search(r'jwt|token|cookie|authorization|email|phone|name|login|card|password|secret|url', k, re.I):
                result[k] = '<redacted>'
            elif k in ('status', 'statusCode', 'authorizationStatus', 'operationStatus', 'saleStatus', 'type', 'paymentWay') and isinstance(v, str) and re.fullmatch(r'[A-Za-z_\-]{1,50}', v):
                result[k] = v
            else:
                result[k] = audit_shape(v, depth + 1)
        return result
    if isinstance(value, list):
        return [audit_shape(v, depth + 1) for v in value[:2]]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return '<number>'
    return '<string>'


def audit_path(url: str) -> str:
    p = urlsplit(url)
    # Retain route shape, not order IDs, path tokens or query values.
    parts = p.path.split('/')
    clean = []
    for i, part in enumerate(parts):
        if i and parts[i - 1] in ('sessions', 'orders', 'order', 'reservations', 'payments', 'user', 'session-order-preview') and part != 'create':
            clean.append('<id>')
        elif len(part) > 32 or part.isdecimal():
            clean.append('<id>')
        else:
            clean.append(part)
    return '/'.join(clean)


class State:
    """Atomic local journal. Never auto-clear an uncertain checkout attempt."""
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(self.data, dict):
                    raise ValueError
            except (ValueError, OSError):
                raise RuntimeError('Повреждён state.json: проверьте незавершённый заказ перед восстановлением.') from None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        import os
        with tmp.open('w', encoding='utf-8') as handle:
            handle.write(json.dumps(self.data, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    def claim(self, seats: list[Seat]) -> str:
        if self.data.get('attempt'):
            raise RuntimeError('Есть незавершённая попытка; новая бронь запрещена.')
        key = hashlib.sha256(('|'.join(s.identity for s in seats) + str(time.time_ns())).encode()).hexdigest()[:20]
        self.data['attempt'] = {'id': key, 'stage': 'STARTED', 'at': time.time(),
                                'seat_ids': [s.identity for s in seats]}
        self.save()
        return key

    def stage(self, value: str) -> None:
        if self.data.get('attempt'):
            self.data['attempt']['stage'] = value
            self.save()
