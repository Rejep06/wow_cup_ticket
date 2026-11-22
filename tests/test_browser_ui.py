"""Optional real Chromium tests with fully mocked/offline content, not the live Yandex widget."""
import os
import unittest
from unittest.mock import patch
from core import extract_seats
from tests.test_core import fixture
from ui_adapter import WidgetUI, UIConfig
from browser_flow import Scope

HTML = '''<!doctype html><html lang="ru"><body>
<div id="order-summary">Сектор C1<br>Ряд 1, место 14<br>Итого: 15 000 ₽</div>
<input name="fullName"><input type="email" name="email"><input type="tel" name="phone">
<label><input type="radio" name="paymentWay" value="new-sbp-token">СБП</label>
<label><input type="radio" name="paymentWay" value="card">Карта</label>
<h2>Способ оплаты</h2><button>Оплатить</button>
</body></html>'''

@unittest.skipUnless(os.environ.get('TEST_BROWSER_PATH'), 'Set TEST_BROWSER_PATH to an installed Chromium for offline UI tests.')
class OfflineBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.p = await async_playwright().start()
        self.addAsyncCleanup(self.p.stop)
        self.browser = await self.p.chromium.launch(executable_path=os.environ['TEST_BROWSER_PATH'], headless=True,
                                                   args=['--no-sandbox'])
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        await self.context.set_offline(True)
        await self.context.route('**/*', lambda route: route.abort())
        self.page = await self.context.new_page()
        # No navigation to an external domain. Test only selectors on an about:blank fixture.
        await self.page.set_content(HTML)
        self.scope = Scope(self.page)
        self.ui = WidgetUI(UIConfig())
        self.ui.widget_frames = lambda context: [self.page.main_frame]
        host = patch('ui_adapter.WIDGET_HOST', '')
        host.start()
        self.addCleanup(host.stop)

    async def test_cart_contact_fill_and_sbp(self):
        seats=extract_seats(fixture())
        self.assertTrue(await self.ui.checked_summary(self.scope,seats,1500000))
        await self.ui.fill_contacts(self.scope,{'name':'Test Buyer','email':'test@example.invalid','phone':'+70000000000'})
        self.assertEqual(await self.page.locator('input[name=fullName]').input_value(),'Test Buyer')
        self.assertTrue(await self.ui.select_new_sbp(self.scope))
        self.assertTrue(await self.page.locator('input[value="new-sbp-token"]').is_checked())
        self.assertFalse(await self.page.locator('input[value="card"]').is_checked())
        self.assertEqual((await self.ui.sbp_start_button(self.scope))[1],'Оплатить')

    async def test_payment_title_not_proof(self):
        self.assertFalse(await self.ui.payment_visible(self.scope))
        self.assertIsNone(await self.ui.next_button(self.scope))
        await self.page.evaluate("document.body.insertAdjacentHTML('beforeend','<input autocomplete=cc-number>')")
        self.assertTrue(await self.ui.payment_visible(self.scope))

    async def test_unknown_layout_blocks(self):
        await self.page.locator('#order-summary').evaluate("e=>e.remove()")
        self.assertIsNone(await self.ui.checked_summary(self.scope,extract_seats(fixture()),1500000))

    async def test_diagnostics_exclude_field_values(self):
        await self.ui.fill_contacts(self.scope,{'name':'Unique Secret Name','email':'private@example.invalid','phone':'+70000000001'})
        import json
        text=json.dumps(await self.ui.inspection(self.scope))
        for secret in ('Unique Secret Name','private@example.invalid','+70000000001'):
            self.assertNotIn(secret,text)
