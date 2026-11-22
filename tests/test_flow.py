import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from core import State, extract_seats
from tests.test_core import fixture, T
from browser_flow import run_checkout, BrowserObserver, Journal
from tests.test_orders import BASE, ORDER_ID, create_response, reserve

class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def execute_flow(self, failed=False):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'data').mkdir()
            state=State(root/'data/state.json')
            cfg=SimpleNamespace(root=root,max_total=1500000,event_id=908192,date='2026-12-05',contacts={},auto_sbp=False)
            page=SimpleNamespace(url=T.url,bring_to_front=AsyncMock())
            ctx=SimpleNamespace(route=AsyncMock(),unroute=AsyncMock(),pages=[page])
            obs=BrowserObserver(cfg,state,Journal(root/'data/diagnostics.jsonl'))
            async def after_goto(*a,**kw):
                obs.tracker.sent(BASE+'create','POST')
                obs.tracker.observe(BASE+'create',create_response())
                obs.tracker.observe(BASE+ORDER_ID+'/operation/reserve',reserve('failed' if failed else 'completed','order-validation-failed' if failed else ''))
                obs.tracker.order_verified=not failed
            page.goto=AsyncMock(side_effect=after_goto)
            ui=SimpleNamespace(captcha_visible=AsyncMock(return_value=False),sold_out=AsyncMock(return_value=False),
                               payment_visible=AsyncMock(return_value=True),inspection=AsyncMock(return_value={}))
            notify=SimpleNamespace(enqueue=Mock())
            result=await run_checkout(ctx,page,T,extract_seats(fixture()),cfg,state,obs,ui,notify)
            return result,state.data,ui,ctx

    async def test_failed_reserve_before_payment_ui(self):
        result,state,ui,ctx=await self.execute_flow(failed=True)
        self.assertEqual(result,'RESERVE_FAILED')
        self.assertEqual(state['attempt']['status_code'],'order-validation-failed')
        ui.payment_visible.assert_not_awaited()
        ctx.unroute.assert_awaited_once()

    async def test_payment_ready_requires_order_and_reserve(self):
        result,state,ui,ctx=await self.execute_flow()
        self.assertEqual(result,'PAYMENT_UI_READY')
        self.assertTrue(state['attempt']['order_verified'])
        self.assertTrue(state['attempt']['reserve_complete'])
