"""Observe the widget's own order workflow; never fabricate a booking or call a mutation API."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, unquote
import re
from core import WIDGET_HOST, Seat, money_value, ProtocolError

PREFIX = '/api/tickets/v2/orders/'


def route_kind(url: str) -> tuple[str, str]:
    p = urlsplit(url)
    if p.scheme != 'https' or p.hostname != WIDGET_HOST:
        return '', ''
    if p.path == PREFIX + 'create':
        return 'create', ''
    match = re.fullmatch(re.escape(PREFIX) + r'([^/]+)(/operation/reserve|/cancel)?', p.path)
    if not match:
        return '', ''
    return {None: 'order', '/operation/reserve': 'reserve', '/cancel': 'cancel'}[match[2]], unquote(match[1])


def verify_order(order: dict, seats: list[Seat], event_id: int, date: str, limit: int) -> bool:
    """Full order evidence must match event, seats, quantity and final amount; IDs alone are insufficient."""
    try:
        session = order['session']
        if str(session['event']['id']) != str(event_id) or not str(session['sessionDate']).startswith(date + 'T'):
            return False
        items = order['items']
        if len(items) != len(seats) or order['totalTicketCount'] != len(seats):
            return False
        expected = sorted((s.source_id, str(s.level_id), s.row, s.place) for s in seats)
        actual = sorted((str(x['sourceSeatId']), str(x['hallplanLevelId']), str(x['row']), str(x['place'])) for x in items)
        if expected != actual or any(x.get('type') != 'seat-ticket' for x in items):
            return False
        total = money_value(order['sum']['total'])
        return (0 < total <= limit and total == sum(s.total for s in seats)
                and not order.get('isCancelled') and not order.get('totalSupplementCount', 0)
                and order.get('insuranceApplied') is not True and order.get('upsaleApplied') is not True)
    except (KeyError, TypeError, ValueError, ProtocolError):
        return False


@dataclass
class OrderTracker:
    order_id: str = ''
    stage: str = 'STARTED'
    status_code: str = ''
    expiry_seconds: int | None = None
    create_sent: bool = False
    duplicate_request: bool = False
    order_verified: bool = False
    reserve_complete: bool = False
    cancel_confirmed: bool = False

    def sent(self, url: str, method: str) -> None:
        kind, _ = route_kind(url)
        if kind == 'create' and method.upper() not in ('GET', 'HEAD', 'OPTIONS'):
            if self.create_sent:
                self.duplicate_request = True
                self.stage = 'MANUAL_REVIEW'
            else:
                self.create_sent = True
                self.stage = 'CREATE_SENT'

    def observe(self, url: str, data: Any) -> str:
        kind, route_id = route_kind(url)
        if not kind or not isinstance(data, dict):
            return self.stage
        result = data.get('result', {})
        if not isinstance(result, dict):
            return self.stage
        if kind == 'create':
            if not self.create_sent:
                return self.stage  # Response from before this attempt is not proof.
            if data.get('status') != 'success':
                self.stage = 'CREATE_FAILED'
                return self.stage
            order = result.get('order', {})
            if not isinstance(order, dict) or not isinstance(order.get('id'), str):
                self.stage = 'MANUAL_REVIEW'
                return self.stage
            if self.order_id and self.order_id != order['id']:
                self.stage = 'MANUAL_REVIEW'
                return self.stage
            self.order_id = order['id']
            self.stage = 'EXISTING_ORDER_REVIEW' if result.get('existingOrder') is True else 'ORDER_CREATED'
            self._expiry(order)
            # booked/approved are intentionally NOT interpreted as a reservation status here.
        elif route_id != self.order_id or not self.order_id:
            return self.stage
        elif kind == 'reserve' and data.get('status') == 'success':
            op = result.get('operation', {})
            if not isinstance(op, dict) or op.get('id') != self.order_id or op.get('type') != 'reserve':
                return self.stage
            self._expiry(op)
            status = op.get('status')
            if status == 'failed':
                self.stage = 'RESERVE_FAILED'
                value = op.get('statusCode', '')
                self.status_code = value if isinstance(value, str) and re.fullmatch(r'[\w\-]{1,100}', value) else 'unknown'
            elif self.stage in ('RESERVE_FAILED', 'MANUAL_REVIEW', 'CANCELLED'):
                return self.stage  # Late/out-of-order responses cannot undo terminal failure.
            elif status == 'running':
                if not self.reserve_complete:
                    self.stage = 'RESERVING'
            elif status in ('completed', 'success'):
                # These successful spellings are candidates, not established by the supplied failed trace.
                self.reserve_complete = True
                self.stage = 'RESERVE_COMPLETED_REPORTED'
            else:
                self.stage = 'UNKNOWN_RESERVE_STATUS'
        elif kind == 'cancel' and data.get('status') == 'success':
            self.cancel_confirmed = True
            self.stage = 'CANCELLED'
        return self.stage

    def _expiry(self, value: dict) -> None:
        n = value.get('expirationSeconds')
        if type(n) is int and n >= 0:
            self.expiry_seconds = n

    def snapshot(self) -> dict:
        return {'stage': self.stage, 'order_id': self.order_id,
                'status_code': self.status_code, 'expiry_seconds': self.expiry_seconds,
                'create_sent': self.create_sent, 'order_verified': self.order_verified,
                'reserve_complete': self.reserve_complete, 'cancel_confirmed': self.cancel_confirmed}


def collect_payment_ways(value: Any) -> set[str]:
    if isinstance(value, dict):
        ways = {str(v) for k, v in value.items() if k == 'paymentWay'}
        for child in value.values():
            ways |= collect_payment_ways(child)
        return ways
    if isinstance(value, list):
        return set().union(*(collect_payment_ways(x) for x in value)) if value else set()
    return set()
