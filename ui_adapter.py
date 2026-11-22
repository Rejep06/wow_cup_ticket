"""Conservative browser adapter. Selectors below are candidates, NOT a verified WOW CUP DOM contract.
Unknown markup causes a manual handoff, never blind clicks or a fabricated reservation.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit
from core import Seat, WIDGET_HOST

# Only navigation/order confirmation. Never click Оплатить, СБП or saved-card actions.
SAFE_BUTTONS = ('Далее', 'Продолжить', 'Оформить заказ', 'Подтвердить заказ',
                'Забронировать', 'Перейти к оплате')
SUMMARY_SELECTORS = [
    '[data-test-id="orderItemsList.root"]',
    '[data-component="OrderItemsList"]',
    '[data-test-id="orderSummary.root"]',
    '[data-testid="cart-summary"]',
    '#order-summary',
]
PAIRS = re.compile(r'ряд\s*[:№]?\s*([\w\-]+)\s*[,;\n]*\s*место\s*[:№]?\s*([\w\-]+)', re.I)
TOTAL = re.compile(r'(?:итого|всего|к оплате)\s*:?\s*(\d[\d\s\u00a0\u202f]*(?:[,.]\d{1,2})?)\s*(?:₽|руб\.?|RUB)', re.I)


@dataclass
class UIConfig:
    summary_selectors: list[str] = field(default_factory=lambda: SUMMARY_SELECTORS.copy())
    total_selector: str = ''
    sbp_option_selector: str = 'input[type="radio"][value="new-sbp-token"]'
    payment_ready_selector: str = ''
    name_selector: str = 'input[name="fullName"], input[name="userFullName"], input[name="fullname"], input[autocomplete="name"]'
    email_selector: str = 'input[type="email"], input[name="email"]'
    phone_selector: str = 'input[type="tel"], input[name="phone"]'
    required_consent_selectors: list[str] = field(default_factory=list)
    safe_button_labels: list[str] = field(default_factory=lambda: list(SAFE_BUTTONS))

    @classmethod
    def load(cls, path: Path) -> 'UIConfig':
        if not path.exists():
            return cls()
        values = json.loads(path.read_text(encoding='utf-8'))
        result = cls(**values)
        if any(x not in SAFE_BUTTONS for x in result.safe_button_labels):
            raise ValueError('ui.json: разрешены только названия навигационных кнопок из SAFE_BUTTONS.')
        if not all(isinstance(x, str) and x.strip() for x in result.summary_selectors):
            raise ValueError('ui.json: некорректные селекторы корзины.')
        return result


def parse_total(text: str) -> int | None:
    totals = set()
    for value in TOTAL.findall(text):
        value = re.sub(r'[\s\u00a0\u202f]', '', value).replace(',', '.')
        totals.add(int(Decimal(value) * 100))
    return next(iter(totals)) if len(totals) == 1 else None


def verify_summary(text: str, seats: list[Seat], total_limit: int) -> bool:
    pairs = PAIRS.findall(text)
    expected = [(s.row.casefold(), s.place.casefold()) for s in seats]
    actual = [(row.casefold(), place.casefold()) for row, place in pairs]
    if sorted(actual) != sorted(expected):
        return False
    if any(s.level.casefold() not in text.casefold() for s in seats):
        return False
    total = parse_total(text)
    return total is not None and total == sum(s.total for s in seats) and total <= total_limit


def permitted_label(text: str) -> bool:
    return ' '.join(text.split()) in SAFE_BUTTONS


class WidgetUI:
    def __init__(self, config: UIConfig):
        self.config = config

    def widget_frames(self, context: Any) -> list[Any]:
        return [f for page in context.pages for f in page.frames
                if urlsplit(f.url).hostname == WIDGET_HOST]

    async def payment_visible(self, context: Any) -> bool:
        # We report a UI handoff, NOT server-side RESERVED. Do not inspect field values.
        for page in context.pages:
            for frame in page.frames:
                host = urlsplit(frame.url).hostname or ''
                if not (host == WIDGET_HOST or host in ('payments.yandex.ru', 'pay.yandex.ru', 'trust.yandex.ru')):
                    continue
                try:
                    inputs = frame.locator('input[autocomplete="cc-number"], input[name="cardNumber"]')
                    for i in range(await inputs.count()):
                        if await inputs.nth(i).is_visible():
                            return True
                    if self.config.payment_ready_selector:
                        loc = frame.locator(self.config.payment_ready_selector)
                        if await loc.count() == 1 and await loc.is_visible():
                            return True
                except Exception:
                    continue
        return False

    async def select_new_sbp(self, context: Any) -> bool:
        """Only a radio whose DOM value is exactly new-sbp-token, never a saved card/bound SBP."""
        found = []
        for frame in self.widget_frames(context):
            loc = frame.locator(self.config.sbp_option_selector)
            for i in range(await loc.count()):
                item = loc.nth(i)
                if (await item.is_visible() and await item.is_enabled()
                    and await item.get_attribute('type') == 'radio'
                    and await item.get_attribute('value') == 'new-sbp-token'):
                    found.append(item)
        if len(found) != 1:
            return False
        if not await found[0].is_checked():
            await found[0].check(timeout=1500)
        return await found[0].is_checked()

    async def sbp_start_button(self, context: Any) -> tuple[Any, str] | None:
        # Caller must validate cart and install the outgoing create request guard first.
        found = []
        for frame in self.widget_frames(context):
            loc = frame.get_by_role('button', name=re.compile(r'^Оплатить(?: СБП)?(?:\s+[\d\s\u00a0\u202f.,]+\s*(?:₽|руб\.?))?$', re.I))
            for i in range(await loc.count()):
                b = loc.nth(i)
                if await b.is_visible() and await b.is_enabled():
                    found.append((b, 'Оплатить СБП' if 'СБП' in (await b.inner_text()) else 'Оплатить'))
        return found[0] if len(found) == 1 else None

    async def captcha_visible(self, context: Any) -> bool:
        for page in context.pages:
            for selector in ('iframe[src*="smartcaptcha"][src*="advanced"]',
                             '[data-testid="captcha-challenge"]'):
                try:
                    loc = page.locator(selector)
                    for i in range(await loc.count()):
                        if await loc.nth(i).is_visible():
                            return True
                except Exception:
                    pass
        return False

    async def sold_out(self, context: Any) -> bool:
        for frame in self.widget_frames(context):
            for text in ('Места уже заняты', 'Билеты проданы', 'Билетов нет'):
                loc = frame.get_by_text(text, exact=True)
                if await loc.count() and await loc.first.is_visible():
                    return True
        return False

    async def fill_contacts(self, context: Any, contacts: dict[str, str]) -> None:
        for frame in self.widget_frames(context):
            for field_name in ('name', 'email', 'phone'):
                value = contacts.get(field_name, '')
                if not value:
                    continue
                loc = frame.locator(getattr(self.config, field_name + '_selector'))
                visible = [loc.nth(i) for i in range(await loc.count()) if await loc.nth(i).is_visible()]
                if len(visible) == 1 and await visible[0].is_editable():
                    if await visible[0].input_value() != value:
                        await visible[0].fill(value, timeout=1500)
                        await visible[0].blur()
            # Only explicitly configured, user-reviewed consents; no blanket checkbox checking.
            for selector in self.config.required_consent_selectors:
                loc = frame.locator(selector)
                if await loc.count() == 1 and await loc.is_visible() and not await loc.is_checked():
                    await loc.check(timeout=1500)

    async def checked_summary(self, context: Any, seats: list[Seat], limit: int) -> str | None:
        for frame in self.widget_frames(context):
            for selector in self.config.summary_selectors:
                loc = frame.locator(selector)
                if await loc.count() != 1 or not await loc.is_visible():
                    continue
                text = await loc.inner_text(timeout=1500)
                if self.config.total_selector:
                    total = frame.locator(self.config.total_selector)
                    if await total.count() == 1 and await total.is_visible():
                        text += '\n' + await total.inner_text(timeout=1500)
                if verify_summary(text, seats, limit):
                    return text
        return None

    async def next_button(self, context: Any) -> tuple[Any, str] | None:
        found = []
        for frame in self.widget_frames(context):
            for label in self.config.safe_button_labels:
                loc = frame.get_by_role('button', name=label, exact=True)
                for i in range(await loc.count()):
                    element = loc.nth(i)
                    if await element.is_visible() and await element.is_enabled():
                        found.append((element, label))
        return found[0] if len(found) == 1 else None

    async def inspection(self, context: Any) -> dict:
        """Structural DOM diagnostic. Never input values or page HTML."""
        output = {'notice': 'Проверьте файл вручную перед передачей; значений полей здесь нет.', 'frames': []}
        for frame in self.widget_frames(context):
            entry = await frame.evaluate('''() => ({
                inputs: [...document.querySelectorAll('input')].map(e => ({
                    type:e.type, name:e.name, id:e.id, autocomplete:e.autocomplete,
                    testid:e.getAttribute('data-test-id') || e.getAttribute('data-testid')
                })),
                buttons: [...document.querySelectorAll('button')].filter(e => e.getClientRects().length).map(e => ({
                    text:e.textContent.trim().slice(0,100), disabled:e.disabled,
                    testid:e.getAttribute('data-test-id') || e.getAttribute('data-testid')
                })),
                testIds:[...document.querySelectorAll('[data-test-id],[data-testid],[data-component]')].map(e => ({
                    tag:e.tagName, testid:e.getAttribute('data-test-id') || e.getAttribute('data-testid'),
                    component:e.getAttribute('data-component')
                })).slice(0,200)
            })''')
            # Names of buttons can sometimes include account info; redact likely contacts.
            for button in entry['buttons']:
                button['text'] = re.sub(r'\S+@\S+', '<email>', button['text'])
                button['text'] = re.sub(r'\+?\d[\d ()\-]{9,}\d', '<number>', button['text'])
            output['frames'].append(entry)
        return output
