import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from core import *
from runtime import ReadAPI, SingleInstance
from browser_flow import BrowserObserver, Journal, Scope
from tests.test_core import fixture, CDN, T

class FakeResponse:
    def __init__(self, data, status=200, headers=None):
        self.status=status
        self.headers=headers or {}
        self.body=AsyncMock(return_value=json.dumps(data).encode())
        self.dispose=AsyncMock()

class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_v4_two_reads(self):
        ctx=SimpleNamespace(request=SimpleNamespace(get=AsyncMock(side_effect=[
            FakeResponse({'status':'completed','url':CDN}),
            FakeResponse({'status':'success','result':{'saleStatus':'available','hallplan':fixture()}})])))
        api=ReadAPI(ctx,10)
        plan=await api.hallplan(T)
        self.assertEqual(plan['availableSeatCount'],1)
        self.assertEqual(api.count,2)
        self.assertEqual(ctx.request.get.call_args_list[1].args[0],CDN)
        self.assertEqual(ctx.request.get.call_args.kwargs['max_redirects'],0)

    async def test_429_retry_after_preserved(self):
        r=FakeResponse({},429,{'retry-after':'7200'})
        ctx=SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=r)))
        api=ReadAPI(ctx,10)
        with self.assertRaises(ApiError) as result:await api.json(T.api_url())
        self.assertEqual(result.exception.retry_after,7200)
        self.assertEqual(api.count,1)
        r.dispose.assert_awaited_once()

    async def test_redirect_not_followed(self):
        ctx=SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=FakeResponse({},302))))
        with self.assertRaises(ApiError):await ReadAPI(ctx,10).json(T.api_url())
        self.assertEqual(ctx.request.get.await_count,1)

    async def test_html_is_unknown(self):
        r=FakeResponse({});r.body.return_value=b'<html>challenge</html>'
        ctx=SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=r)))
        with self.assertRaises(ApiError):await ReadAPI(ctx,10).json(T.api_url())

    async def test_antibot_is_pause(self):
        ctx=SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=FakeResponse({'captchaRequired':True}))))
        with self.assertRaises(ApiError) as result:await ReadAPI(ctx,10).json(T.api_url())
        self.assertEqual(result.exception.status,403)

    async def test_pending_is_bounded(self):
        ctx=SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=FakeResponse({'status':'running'}))))
        with patch('runtime.asyncio.sleep',new_callable=AsyncMock):
            with self.assertRaises(ProtocolError):await ReadAPI(ctx,10).hallplan(T)
        self.assertEqual(ctx.request.get.await_count,3)

    async def test_disallow_order_mutation_in_read_client(self):
        ctx=SimpleNamespace(request=SimpleNamespace(get=AsyncMock()))
        with self.assertRaises(ProtocolError):await ReadAPI(ctx,10).json('https://widget.afisha.yandex.ru/api/tickets/v2/orders/create')
        ctx.request.get.assert_not_awaited()

    async def check_guard(self, payload, twice=False, armed=True):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);state=State(root/'state.json')
            cfg=SimpleNamespace()
            obs=BrowserObserver(cfg,state,Journal(root/'diag.jsonl'))
            page=object();obs.start(page,[]);obs.submission_armed=armed
            req=SimpleNamespace(method='POST',post_data_json=payload,frame=SimpleNamespace(page=page))
            route=SimpleNamespace(request=req,continue_=AsyncMock(),abort=AsyncMock())
            await obs.guard(route)
            if twice:await obs.guard(route)
            return route

    async def test_sbp_guard_allows_one(self):
        route=await self.check_guard({'paymentWay':'new-sbp-token'})
        route.continue_.assert_awaited_once();route.abort.assert_not_awaited()

    async def test_sbp_guard_rejects_second(self):
        route=await self.check_guard({'paymentWay':'new-sbp-token'},twice=True)
        route.continue_.assert_awaited_once();route.abort.assert_awaited_once()

    async def test_sbp_guard_rejects_cards(self):
        route=await self.check_guard({'paymentWay':'card'})
        route.continue_.assert_not_awaited();route.abort.assert_awaited_once()

    async def test_sbp_guard_rejects_unknown_schema(self):
        route=await self.check_guard({'nested':{'paymentWay':'new-sbp-token'}})
        route.continue_.assert_not_awaited();route.abort.assert_awaited_once()

    async def test_sbp_guard_rejects_saved_payment(self):
        route=await self.check_guard({'paymentWay':'new-sbp-token','paymentMethodId':'saved-token'})
        route.continue_.assert_not_awaited();route.abort.assert_awaited_once()

    async def test_sbp_guard_requires_arm(self):
        route=await self.check_guard({'paymentWay':'new-sbp-token'},armed=False)
        route.continue_.assert_not_awaited();route.abort.assert_awaited_once()

class LockTests(unittest.TestCase):
    def test_single_process_lock(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'lock';a=SingleInstance(path)
            try:
                with self.assertRaises(RuntimeError):SingleInstance(path)
            finally:a.close()
            b=SingleInstance(path);b.close()
