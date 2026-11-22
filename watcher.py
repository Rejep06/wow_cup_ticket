#!/usr/bin/env python3
"""WOW CUP checkout assistant v6. Experimental widget adapter; see README before enabling."""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import random
import sys
import time
from dotenv import load_dotenv
import httpx
from playwright.async_api import async_playwright
from core import (State, Target, ApiError, ProtocolError, validate_session, extract_seats,
                  choose_seats, failure_delay, discover_ticket_url)
from runtime import Config, SingleInstance, ReadAPI, Notifier
from browser_flow import BrowserObserver, Journal, Scope, run_checkout, safe_inspect
from ui_adapter import WidgetUI, UIConfig

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger('tickets')


async def console(prompt: str) -> str:
    try:
        return (await asyncio.to_thread(input, prompt)).strip()
    except EOFError:
        LOG.warning('Нет интерактивного терминала. Автоматические действия остановлены; Ctrl+C завершает процесс.')
        await asyncio.Event().wait()
        return ''


async def discover(target: Target, state: State, cfg: Config) -> Target:
    now = time.time()
    if now - state.data.get('last_discovery', 0) < cfg.discovery_interval:
        return target
    state.data['last_discovery'] = now
    state.save()
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get('https://wowcup.tennis/')
            response.raise_for_status()
            if len(response.content) > 3_000_000:
                raise ProtocolError('Страница слишком большая.')
            found = Target.parse(discover_ticket_url(response.text))
        if found != target:
            LOG.info('Ссылка официального сайта изменилась. Событие и дата будут проверены перед выбором мест.')
            state.data['ticket_url'] = found.url
            state.save()
            return found
    except Exception as exc:
        LOG.warning('Ссылка не обновлена: %s; используется последний адрес.', type(exc).__name__)
    return target


async def handoff(context, observer, ui, state, notify, message: str) -> None:
    """No background auto-checkout while the owner is solving CAPTCHA or paying."""
    notify.enqueue('handoff-' + str(time.time_ns()), message, private=True)
    print('\n' + message)
    print('Окно браузера остаётся открытым. Не удаляйте data/state.json для повторных заказов.')
    while context.pages:
        value = await console('Enter: сохранить диагностику; q: закрыть браузер и завершить: ')
        await observer.flush()
        await safe_inspect(ui, observer.scope or Scope(context.pages[0]), ROOT / 'data' / 'ui-structure.json')
        if value.lower() == 'q':
            return


async def watch(context, page, cfg, state, observer, ui, notify):
    api = ReadAPI(context, cfg.timeout)
    target = Target.parse(state.data.get('ticket_url', cfg.target.url))
    if state.data.get('attempt'):
        observer.scope = Scope(page)
        await handoff(context, observer, ui, state, notify,
            'В журнале есть незавершённая попытка. Новые заказы заблокированы. '
            'Проверьте заказ вручную; --reset-attempt не отменяет его на сервере.')
        return
    cfg.validate_automation()
    notify.enqueue('started-' + str(time.time_ns()),
        f'Мониторинг запущен. Интервал не меньше {cfg.interval:g} с. '
        f'Автооформление: {cfg.auto_booking}. Оплату подтверждает человек.')
    last_fingerprint = None
    warmed = False
    while not page.is_closed():
        due = max(float(state.data.get('next_check_at', 0)), observer.blocked_until)
        if due > time.time():
            await asyncio.sleep(min(5, due - time.time()))
            continue
        if observer.challenge or await ui.captcha_visible(Scope(page)):
            notify.enqueue('challenge-' + str(time.time_ns()),
                'CAPTCHA/ограничение: автоматический опрос приостановлен. Откройте браузер бота.', private=True)
            print('Пройдите проверку вручную в окне бота. Скрипт не решает CAPTCHA.')
            if (await console('После проверки Enter для продолжения, q для выхода: ')).lower() == 'q':
                return
            observer.challenge = False  # Explicit human resume; if challenge remains it will stop again.
            continue
        start = time.monotonic()
        try:
            target = await discover(target, state, cfg)
            if cfg.auto_booking and not warmed:
                await page.goto(target.url, wait_until='domcontentloaded', timeout=25000)
                warmed = True
                # Initial browser responses may still be arriving; no checkout bypasses their CAPTCHA.
                await asyncio.sleep(.2)
                if observer.challenge or await ui.captcha_visible(Scope(page)):
                    continue
            session = validate_session(await api.json(target.api_url()), cfg.event_id, cfg.date)
            event_time = datetime.fromisoformat(session['sessionDate'])
            if event_time.tzinfo is None:
                raise ProtocolError('sessionDate без часового пояса.')
            if event_time <= datetime.now(timezone.utc):
                LOG.info('Сеанс уже начался; мониторинг остановлен.')
                return
            candidate = []
            if session['saleStatus'] == 'available' and session['availableSeatCount'] > 0:
                canonical = Target(session['key'], target.client_key, target.region_id)
                plan = await api.hallplan(canonical)
                seats = extract_seats(plan)
                services = {str(x.get('extSessionId')) for x in session.get('sessionServices', [])}
                if any(str(s.ext_session_id) not in services for s in seats):
                    raise ProtocolError('Идентификаторы поставщика устарели; схема не соответствует сеансу.')
                per_price = cfg.max_price or 10**15
                total_price = cfg.max_total or 10**15
                candidate = choose_seats(seats, cfg.quantity, per_price, total_price,
                    min(session['maxTicketsCount'], int(plan.get('maxTicketsCount', session['maxTicketsCount']))),
                    cfg.sectors, cfg.adjacent)
                if candidate:
                    fingerprint = '|'.join(s.identity for s in candidate)
                    if cfg.auto_booking:
                        # No confirmation delay, no Telegram await, no browser launch here.
                        result = await run_checkout(context, page, canonical, candidate, cfg, state, observer, ui, notify)
                        await handoff(context, observer, ui, state, notify,
                            f'Результат: {result}. Браузер передан вам. '
                            'Автоматический опрос и новые заказы остановлены.')
                        return
                    if fingerprint != last_fingerprint:
                        notify.enqueue('seats-' + str(time.time_ns()),
                            f'Динамическая схема показывает {len(candidate)} подходящих мест. '
                            'Это не бронь.\n' + canonical.selected_url(candidate))
                        last_fingerprint = fingerprint
            if not candidate:
                last_fingerprint = None
            if state.data.get('consecutive_errors', 0):
                notify.enqueue('recovery-' + str(time.time_ns()), 'Чтение API восстановилось; обычный интервал возвращён.')
            state.data['consecutive_errors'] = 0
            state.data['last_success'] = time.time()
            period = cfg.interval * (1 + random.uniform(0, cfg.jitter))
            # Start-to-start scheduling without overlapping or catch-up bursts; 1 s minimum after a slow response.
            delay = max(1, period - (time.monotonic() - start))
            LOG.info('API: availableSeatCount=%s; подходящих=%s; GET всего=%s; следующая проверка через %.1f с',
                     session['availableSeatCount'], len(candidate), api.count, delay)
        except (ApiError, ProtocolError) as exc:
            errors = int(state.data.get('consecutive_errors', 0)) + 1
            state.data['consecutive_errors'] = errors
            http_status = getattr(exc, 'status', 0)
            delay = failure_delay(errors, retry_after=getattr(exc, 'retry_after', 0), rate_limited=http_status == 429)
            if http_status in (401, 403):
                observer.challenge = True
                delay = max(300, delay)
            # No route-guessing/fallback requests on 403/429. Discovery is independently rate-limited.
            if errors == 1 or errors == 3:
                notify.enqueue('error-' + str(time.time_ns()),
                    f'Проверка API не удалась ({type(exc).__name__}, HTTP {http_status or "—"}). '
                    f'Пауза {delay:.0f} с. Наличие билетов неизвестно.')
            LOG.warning('%s; пауза %.0f с', str(exc), delay)
        except Exception as exc:
            # Browser or unexpected schema/program failure is NOT a sold-out result.
            LOG.error('Непредвиденная ошибка %s. Автооформление остановлено.', type(exc).__name__)
            await handoff(context, observer, ui, state, notify,
                          'Непредвиденная ошибка браузера/протокола. Требуется ручная проверка.')
            return
        state.data['next_check_at'] = time.time() + delay
        state.save()


async def main(args):
    load_dotenv(ROOT / '.env')
    cfg = Config.load(ROOT)
    data_dir = ROOT / 'data'
    data_dir.mkdir(exist_ok=True)
    lock = SingleInstance(data_dir / 'instance.lock')
    notify = None
    try:
        state = State(data_dir / 'state.json')
        if args.reset_attempt:
            print('Это очистит только локальный запрет повторной попытки. Заказ на сервере НЕ отменяется.')
            if await console('Проверьте неоплаченные заказы. Введите ПРОВЕРЕНО для сброса: ') == 'ПРОВЕРЕНО':
                state.data.pop('attempt', None)
                state.save()
                print('Локальная попытка сброшена; серверные ограничения и backoff сохранены.')
            return
        notify = Notifier(data_dir / 'outbox.json', cfg.token, cfg.chats, cfg.owner)
        notify.start()
        if args.test:
            if not cfg.token or not cfg.chats:
                raise ValueError('Для теста заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_IDS.')
            notify.enqueue('test-' + str(time.time_ns()), 'Тест доставки v6. Сообщение не означает наличие или бронь билета.')
            deadline = time.monotonic() + 12
            while notify.items and time.monotonic() < deadline:
                await asyncio.sleep(.2)
            print('Доставка завершена.' if not notify.items else 'Есть недоставленные сообщения; очередь сохранена.')
            return
        ui = WidgetUI(UIConfig.load(ROOT / 'ui.json'))
        observer = BrowserObserver(cfg, state, Journal(data_dir / 'diagnostics.jsonl'))
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(str(data_dir / 'browser-profile'),
                headless=cfg.headless, viewport={'width': 1280, 'height': 900}, locale='ru-RU')
            context.set_default_timeout(2500)
            observer.attach(context)
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                if args.setup or args.capture or args.inspect:
                    observer.scope = Scope(page)
                    await page.goto(cfg.target.url, wait_until='domcontentloaded', timeout=25000)
                    print('Ручной режим: скрипт не выбирает места и не нажимает кнопки.')
                    print('Профиль локальный. Входите в аккаунт и проходите CAPTCHA только в этом окне.')
                    await console('Enter после нужного шага: сохранить структуру/закрыть браузер: ')
                    await observer.flush()
                    await safe_inspect(ui, observer.scope, data_dir / 'ui-structure.json')
                    print('Диагностика: data/diagnostics.jsonl и data/ui-structure.json. Проверьте их перед передачей.')
                elif args.check:
                    api = ReadAPI(context, cfg.timeout)
                    s = validate_session(await api.json(cfg.target.api_url()), cfg.event_id, cfg.date)
                    print(json.dumps({'sessionId': s.get('id'), 'date': s['sessionDate'],
                                      'saleStatus': s['saleStatus'], 'availableSeatCount': s['availableSeatCount']}, ensure_ascii=False, indent=2))
                    if s['availableSeatCount'] > 0 and s['saleStatus'] == 'available':
                        plan = await api.hallplan(Target(s['key'], cfg.target.client_key, cfg.target.region_id))
                        for seat in extract_seats(plan):
                            print(f'{seat.level}; ряд {seat.row}; место {seat.place}; с комиссией {seat.total / 100:.2f} RUB')
                else:
                    await watch(context, page, cfg, state, observer, ui, notify)
            finally:
                await observer.remove_guard(context)
                await observer.flush()
                await context.close()
    finally:
        if notify:
            await notify.close()
        lock.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    # Never print httpx URLs: a Telegram URL embeds the bot token.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--setup', action='store_true', help='Подготовить локальный браузер вручную')
    group.add_argument('--capture', action='store_true', help='Ручной проход с обезличенной диагностикой')
    group.add_argument('--inspect', action='store_true', help='Сохранить структуру интерфейса')
    group.add_argument('--check', '--seats', action='store_true', help='Однократное чтение, без оформления')
    group.add_argument('--test', action='store_true', help='Тест Telegram')
    group.add_argument('--reset-attempt', action='store_true', help='Ручной сброс локальной блокировки')
    try:
        asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        print('\nОстановлено. Незавершённая попытка остаётся в локальном журнале.')
    except (ValueError, RuntimeError, ProtocolError) as exc:
        print(f'Ошибка: {exc}', file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f'Ошибка {type(exc).__name__}. Проверьте окружение, браузер и конфигурацию.', file=sys.stderr)
        sys.exit(2)
