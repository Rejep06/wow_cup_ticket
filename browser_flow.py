"""Browser-assisted checkout. The widget builds all mutation requests itself.
No replayed JWT, no CAPTCHA solver, no direct orders/create/reserve/cancel implementation.
"""
from __future__ import annotations
import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, unquote
from core import State, Seat, Target, WIDGET_HOST, audit_path, audit_shape, is_challenge, retry_after_seconds
from order_state import OrderTracker, route_kind, verify_order, collect_payment_ways
from ui_adapter import UIConfig, WidgetUI, verify_summary
from runtime import Config, Notifier

LOG = logging.getLogger('tickets')
CREATE_ROUTE = re.compile(r'^https://widget\.afisha\.yandex\.ru/api/tickets/v2/orders/create(?:\?.*)?$')
RELEVANT = ('/api/antibot/', '/api/session-order-preview', '/api/tickets/v2/orders/')


class Scope:
    def __init__(self, page: Any):
        self.pages = [page]


class Journal:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, value: dict) -> None:
        # Limited, anonymised shapes only. No headers, raw payloads, URLs or response body logs.
        if self.path.exists() and self.path.stat().st_size > 2_000_000:
            self.path.replace(self.path.with_suffix('.previous.jsonl'))
        with self.path.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': time.time(), **value}, ensure_ascii=False) + '\n')
        try:
            self.path.chmod(0o600)
        except OSError:
            pass


class BrowserObserver:
    def __init__(self, cfg: Config, state: State, journal: Journal):
        self.cfg, self.state, self.journal = cfg, state, journal
        self.tracker = OrderTracker()
        self.seats: list[Seat] = []
        self.active = False
        self.scope: Scope | None = None
        self.challenge = False
        self.blocked_until = 0.0
        self.error = ''
        self.changed = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        self.seen_create = False
        self.submission_armed = False
        self.guard_installed = False
        self.epoch = 0

    def attach(self, context: Any) -> None:
        context.on('request', self.on_request)
        context.on('response', self.on_response_event)
        context.on('page', self.on_page)

    def on_page(self, page: Any) -> None:
        if self.active and self.scope and page not in self.scope.pages:
            self.scope.pages.append(page)

    def belongs(self, request: Any) -> bool:
        try:
            return bool(self.scope and request.frame.page in self.scope.pages)
        except Exception:
            return False

    def start(self, page: Any, seats: list[Seat]) -> None:
        self.epoch += 1
        self.scope = Scope(page)
        self.seats = seats
        self.active = True
        self.tracker = OrderTracker()
        self.seen_create = False
        self.submission_armed = False
        self.error = ''

    def on_request(self, request: Any) -> None:
        path = urlsplit(request.url).path
        if urlsplit(request.url).hostname != WIDGET_HOST or not path.startswith(RELEVANT):
            return
        try:
            payload = request.post_data_json
        except Exception:
            payload = '<not-json-or-empty>'
        self.journal.add({'event': 'request', 'path': audit_path(request.url),
                          'method': request.method, 'payload_shape': audit_shape(payload)})
        if self.active and self.belongs(request):
            before = self.tracker.stage
            self.tracker.sent(request.url, request.method)
            if self.tracker.stage != before:
                self.persist()
            self.changed.set()

    def on_response_event(self, response: Any) -> None:
        epoch, relevant_attempt = self.epoch, self.active and self.belongs(response.request)
        task = asyncio.create_task(self.on_response(response, epoch, relevant_attempt))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def on_response(self, response: Any, epoch: int, relevant_attempt: bool) -> None:
        try:
            p = urlsplit(response.url)
            if p.hostname != WIDGET_HOST or not p.path.startswith('/api/'):
                return
            if response.status in (401, 403, 429):
                if response.status in (401, 403):
                    self.challenge = True
                self.blocked_until = max(self.blocked_until, time.time() + max(300, retry_after_seconds(response.headers.get('retry-after'))))
                self.changed.set()
            if not p.path.startswith(RELEVANT):
                return
            try:
                body = await response.body()
                if len(body) > 1_000_000:
                    return
                data = json.loads(body)
            except Exception:
                self.journal.add({'event': 'response', 'path': audit_path(response.url), 'http': response.status, 'body': '<not-json>'})
                return
            self.journal.add({'event': 'response', 'path': audit_path(response.url),
                              'http': response.status, 'shape': audit_shape(data)})
            if is_challenge(data):
                self.challenge = True
                self.changed.set()
            if p.path.startswith('/api/antibot/') and isinstance(data, dict) and data.get('captchaRequired') is False:
                self.challenge = False
                self.changed.set()
            if not self.active or epoch != self.epoch or not relevant_attempt:
                return
            before = self.tracker.snapshot()
            self.tracker.observe(response.url, data)
            kind, oid = route_kind(response.url)
            if kind == 'order' and oid == self.tracker.order_id and isinstance(data, dict) and data.get('status') == 'success':
                order = data.get('result', {}).get('order', {})
                self.tracker.order_verified = verify_order(order, self.seats, self.cfg.event_id, self.cfg.date, self.cfg.max_total)
                if not self.tracker.order_verified:
                    self.error = 'ORDER_MISMATCH'
            if before != self.tracker.snapshot():
                self.persist()
            self.changed.set()
        except Exception as exc:
            LOG.warning('Ошибка наблюдателя: %s; результат заказа не считается успешным.', type(exc).__name__)
            if self.active:
                self.error = 'OBSERVER_ERROR'
                self.changed.set()

    def persist(self) -> None:
        attempt = self.state.data.get('attempt')
        if attempt:
            attempt.update(self.tracker.snapshot())
            self.state.save()

    async def guard(self, route: Any) -> None:
        """Permit only one widget-originated new-SBP order, after checked UI and explicit user opt-in.
        Unknown payload -> stop, never guess fields or inject an antibot token.
        """
        request = route.request
        if request.method == 'OPTIONS':
            await route.continue_()
            return
        try:
            body = request.post_data_json
        except Exception:
            body = None
        allowed = (self.active and self.belongs(request) and self.submission_armed and
                   request.method == 'POST' and not self.seen_create and
                   isinstance(body, dict) and body.get('paymentWay') == 'new-sbp-token' and
                   collect_payment_ways(body) == {'new-sbp-token'} and
                   not any(body.get(k) for k in ('paymentMethodId', 'attemptedPaymentMethodId', 'cardId', 'boundCardId')))
        if not allowed:
            self.error = 'CREATE_GUARD_STOP'
            self.journal.add({'event': 'guard-stop', 'why': 'duplicate, unarmed or unknown/non-new-SBP payload'})
            self.changed.set()
            await route.abort('blockedbyclient')
            return
        self.seen_create = True  # Before the HTTP request; no retry after an ambiguous transport failure.
        await route.continue_()

    async def install_guard(self, context: Any) -> None:
        await context.route(CREATE_ROUTE, self.guard)
        self.guard_installed = True

    async def remove_guard(self, context: Any) -> None:
        if self.guard_installed:
            await context.unroute(CREATE_ROUTE, self.guard)
            self.guard_installed = False
        self.submission_armed = False

    async def flush(self) -> None:
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


async def safe_inspect(ui: WidgetUI, scope: Scope, path: Path) -> None:
    try:
        value = await ui.inspection(scope)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        path.chmod(0o600)
    except Exception:
        LOG.warning('Не удалось сохранить структуру DOM.')


async def run_checkout(context: Any, page: Any, target: Target, seats: list[Seat], cfg: Config,
                       state: State, observer: BrowserObserver, ui: WidgetUI, notify: Notifier) -> str:
    attempt_id = state.claim(seats)  # Durable before navigation/clicks.
    observer.start(page, seats)
    await observer.install_guard(context)
    status = 'MANUAL_REVIEW'
    try:
        # Same persistent browser; no per-candidate Chromium launch or second confirmation GET.
        await page.goto(target.selected_url(seats), wait_until='domcontentloaded', timeout=25000)
        await page.bring_to_front()
        notify.enqueue('attempt-' + attempt_id, 'Найдены подходящие места. Начата попытка оформления; бронь ещё не подтверждена.')
        deadline = time.monotonic() + 60
        last_clicked: set[tuple[str, str]] = set()
        last_progress = time.monotonic()
        while time.monotonic() < deadline:
            # Failures take precedence over payment-looking UI.
            if observer.error:
                status = observer.error
                break
            stage = observer.tracker.stage
            if stage in ('RESERVE_FAILED', 'CREATE_FAILED', 'CANCELLED', 'EXISTING_ORDER_REVIEW', 'UNKNOWN_RESERVE_STATUS', 'MANUAL_REVIEW'):
                status = stage
                break
            if observer.challenge or await ui.captcha_visible(observer.scope):
                state.stage('CAPTCHA_REQUIRED')
                notify.enqueue('captcha-' + attempt_id,
                    'Нужна CAPTCHA. Пройдите её вручную в браузере бота; затем Enter в терминале.', private=True)
                await page.bring_to_front()
                try:
                    answer = await asyncio.to_thread(input, 'Пройдите CAPTCHA. Enter продолжить, q передать управление: ')
                except EOFError:
                    answer = 'q'
                if answer.strip().lower() == 'q' or await ui.captcha_visible(observer.scope):
                    status = 'CAPTCHA_REQUIRED'
                    break
                observer.challenge = False
                deadline = time.monotonic() + 60
                last_progress = time.monotonic()
                continue
            if observer.blocked_until > time.time():
                status = 'RATE_LIMITED'
                break
            if await ui.sold_out(observer.scope):
                status = 'SEATS_UNAVAILABLE'
                break
            if await ui.payment_visible(observer.scope):
                # Actual card/QR input view, not merely the title "способ оплаты".
                status = ('PAYMENT_UI_READY' if observer.tracker.order_id and observer.tracker.order_verified
                          and observer.tracker.reserve_complete else 'PAYMENT_UI_UNVERIFIED')
                break
            if observer.tracker.create_sent:
                # The official widget performs reserve/status requests. Never re-click Submit.
                await wait_for_change(observer, .25)
                continue
            await ui.fill_contacts(observer.scope, cfg.contacts)
            summary = await ui.checked_summary(observer.scope, seats, cfg.max_total)
            if summary:
                selected_sbp = await ui.select_new_sbp(observer.scope) if cfg.auto_sbp else False
                sbp_button = await ui.sbp_start_button(observer.scope) if cfg.auto_sbp else None
                dedicated_sbp = bool(sbp_button and sbp_button[1] == 'Оплатить СБП')
                # A dedicated SBP button is also an explicit, recognisable payment-method choice.
                observer.submission_armed = cfg.auto_sbp and (selected_sbp or dedicated_sbp)
                button = await ui.next_button(observer.scope)
                if not button and observer.submission_armed:
                    button = sbp_button
                if button:
                    element, label = button
                    step_key = (page.url.split('?')[0], label)
                    if step_key not in last_clicked:
                        if label.startswith('Оплатить') and not observer.submission_armed:
                            status = 'MANUAL_PAYMENT_BUTTON'
                            break
                        # Last-check protects against a cart changed by the frontend.
                        if not await ui.checked_summary(observer.scope, seats, cfg.max_total):
                            status = 'CART_CHANGED'
                            break
                        last_clicked.add(step_key)  # Never blind-retry a click that might have submitted.
                        state.stage('UI_CLICK_' + label)
                        await element.click(timeout=2500)
                        last_progress = time.monotonic()
                        continue
            if time.monotonic() - last_progress > 12:
                status = 'NEEDS_UI_ADAPTER'
                break
            await wait_for_change(observer, .2)  # Local event wait, not an API request.
        else:
            status = ('RESERVE_REPORTED_PAYMENT_UI_NOT_RECOGNIZED' if observer.tracker.reserve_complete and observer.tracker.order_verified
                      else 'ORDER_RESULT_UNKNOWN' if observer.tracker.create_sent else 'NEEDS_UI_ADAPTER')
    except Exception as exc:
        LOG.warning('Оформление остановлено: %s. Повторное создание заказа не выполняется.', type(exc).__name__)
        status = 'ORDER_RESULT_UNKNOWN' if observer.tracker.create_sent else 'BROWSER_ERROR'
    finally:
        # Persist uncertainty even when Telegram fails or the process is restarted.
        observer.persist()
        state.stage(status)
        await safe_inspect(ui, observer.scope, cfg.root / 'data' / 'ui-structure.json')
        if observer.tracker.order_id:
            for p in observer.scope.pages:
                try:
                    u = urlsplit(p.url)
                    if u.hostname == WIDGET_HOST and observer.tracker.order_id in unquote(u.path):
                        state.data['attempt']['observed_page_url'] = p.url
                        state.save()
                except Exception:
                    pass
        code = observer.tracker.status_code
        notify.enqueue('result-' + attempt_id,
            f'Оформление: {status}' + (f' ({code})' if code else '') +
            '\nОткройте окно браузера бота. Автоматической оплаты и новой попытки не будет.', private=True)
        # Hand back normal browser control to the owner; journal still prevents unattended re-submission.
        await observer.remove_guard(context)
    return status


async def wait_for_change(observer: BrowserObserver, timeout: float) -> None:
    try:
        await asyncio.wait_for(observer.changed.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    observer.changed.clear()
