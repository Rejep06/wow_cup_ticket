import copy
import json
from pathlib import Path
import tempfile
import unittest
from core import *
from order_state import *
from ui_adapter import verify_summary, parse_total, permitted_label

T = Target.parse(DEFAULT_TICKET_URL)
CDN = 'https://example.svc.cdn.yandex.net/hallplans/' + 'a'*64

def fixture():
    return {'availableSeatCount': 1, 'maxTicketsCount': 2, 'levels': [
        {'id': 5, 'name': 'Сектор C1', 'admission': False, 'seats': [
            {'seat': {'row': '1', 'place': '14', 'xCoord': 1879, 'yCoord': 1577, 'linkedSeats': []},
             'sourceSeatId': 'seat-test-14', 'serviceId': 'ticketsteam-8261', 'gatewaySessionId': '77813737',
             'extSessionId': 379278197, 'levelId': 5, 'admission': False,
             'priceInfo': {'price': {'currencyCode': 'rub', 'value': 1500000},
                           'total': {'currencyCode': 'rub', 'value': 1500000}}}]}]}

def session():
    return {'status':'success', 'result':{'session':{'key':T.key, 'event':{'id':908192},
        'sessionDate':'2026-12-05T17:30+03:00','availableSeatCount':1,'maxTicketsCount':2, 'saleStatus':'available'}}}

class PureTests(unittest.TestCase):
    def setUp(self):
        self.plan = fixture()
        self.seats = extract_seats(self.plan)

    def test_alias_key(self):
        alias = Target.parse('https://widget.afisha.yandex.ru/w/sessions/ticketsteam-8261@77813737?clientKey=test')
        self.assertEqual(alias.key,'ticketsteam-8261@77813737')
        self.assertNotEqual(alias.key, validate_session(session(),908192,'2026-12-05')['key'])

    def test_target_disallows_other_host(self):
        for url in ('http://widget.afisha.yandex.ru/w/sessions/test?clientKey=x',
                    'https://widget.afisha.yandex.ru.evil.test/w/sessions/test?clientKey=x',
                    'https://widget.afisha.yandex.ru@evil.test/w/sessions/test?clientKey=x'):
            with self.assertRaises(ProtocolError): Target.parse(url)

    def test_discovery_nuxt(self):
        self.assertEqual(discover_ticket_url('window.__NUXT__={public:{ticketUrl:"'+T.url.replace('&','&amp;')+'"}}'), T.url)

    def test_discovery_ambiguity(self):
        with self.assertRaises(ProtocolError):
            discover_ticket_url(T.url+' '+T.url.replace(T.key,'different'))

    def test_session_wrong_event(self):
        with self.assertRaises(ProtocolError): validate_session(session(),1,'2026-12-05')

    def test_session_wrong_date(self):
        with self.assertRaises(ProtocolError): validate_session(session(),908192,'2026-12-06')

    def test_count_bool_is_not_integer(self):
        data=session();data['result']['session']['availableSeatCount']=True
        with self.assertRaises(ProtocolError): validate_session(data,908192,'2026-12-05')

    def test_broken_result(self):
        with self.assertRaises(ProtocolError): validate_session({'status':'success','result':[]},908192,'2026-12-05')

    def test_antibot_is_not_no_seats(self):
        with self.assertRaises(ApiError): validate_session({'captchaRequired':True},908192,'2026-12-05')

    def test_v4_pointer(self):
        self.assertEqual(unwrap_hallplan({'status':'completed','url':CDN}),('cdn',CDN))

    def test_bad_pointer_rejected(self):
        with self.assertRaises(ProtocolError): unwrap_hallplan({'status':'completed','url':'http://127.0.0.1/private'})

    def test_false_cdn_suffix(self):
        self.assertFalse(allowed_cdn_url(CDN.replace('.net/', '.net.evil.test/')))

    def test_v4_dynamic(self):
        self.assertEqual(unwrap_hallplan({'status':'success','result':{'saleStatus':'available','operationStatus':'completed','hallplan':self.plan}})[0],'ready')

    def test_running_is_pending(self):
        self.assertEqual(unwrap_hallplan({'status':'running'})[0],'pending')

    def test_static_map_is_not_inventory(self):
        with self.assertRaises(ProtocolError): extract_seats({'levels': [{'id':5,'seats':[{'row':'1','place':'14'}]}]})

    def test_price_minor(self):
        self.assertEqual(self.seats[0].total,1500000)
        self.assertEqual(rub_to_minor('15000'),1500000)
        self.assertEqual(rub_to_minor('12,34'),1234)

    def test_price_validation(self):
        for price in ('NaN','Infinity','-1','0','1.001'):
            with self.assertRaises(ValueError): rub_to_minor(price)

    def test_currency(self):
        self.plan['levels'][0]['seats'][0]['priceInfo']['price']['currencyCode']='usd'
        with self.assertRaises(ProtocolError): extract_seats(self.plan)

    def test_linked_bundles_skipped(self):
        self.plan['levels'][0]['seats'][0]['seat']['linkedSeats']=['2']
        self.assertEqual(extract_seats(self.plan),[])

    def test_bad_seat_object(self):
        self.plan['levels'][0]['seats'][0]['seat']=None
        with self.assertRaises(ProtocolError): extract_seats(self.plan)

    def test_budget_strict(self):
        self.assertEqual(choose_seats(self.seats,1,1499999,2000000),[])
        self.assertEqual(choose_seats(self.seats,1,2000000,1499999),[])
        self.assertEqual(len(choose_seats(self.seats,1,1500000,1500000)),1)

    def test_limits_quantity(self):
        self.assertEqual(choose_seats(self.seats,2,1500000,3000000,max_allowed=1),[])

    def test_sector(self):
        self.assertEqual(choose_seats(self.seats,1,1500000,1500000,sectors=('Сектор C2',)),[])

    def test_adjacency_aisle(self):
        from dataclasses import replace
        b=replace(self.seats[0],source_id='second',place='15',x=1900)
        self.assertEqual(len(choose_seats(self.seats+[b],2,1500000,3000000)),2)
        b=replace(b,x=2100)
        self.assertEqual(choose_seats(self.seats+[b],2,1500000,3000000),[])

    def test_selected_url(self):
        from urllib.parse import urlsplit, parse_qs
        raw=parse_qs(urlsplit(T.selected_url(self.seats)).query)['selectedSeats'][0]
        self.assertEqual(json.loads(raw),[{'level':5,'row':'1','place':'14'}])

    def test_backoff_cap(self):
        self.assertEqual([failure_delay(n) for n in range(1,8)],[120,240,480,960,1920,3600,3600])

    def test_retry_after_above_hour(self):
        self.assertEqual(failure_delay(1,retry_after=7200),7200)
        self.assertGreaterEqual(failure_delay(1,rate_limited=True),300)

    def test_retry_date(self):
        self.assertEqual(retry_after_seconds('Thu, 01 Jan 1970 00:02:00 GMT',0),120)
        self.assertEqual(retry_after_seconds('NaN',0),0)

    def test_summary(self):
        self.assertTrue(verify_summary('Сектор C1\nРяд 1, место 14\nИтого: 15 000 ₽',self.seats,1500000))
        self.assertFalse(verify_summary('Сектор C1\nРяд 1, место 15\nИтого: 15 000 ₽',self.seats,1500000))

    def test_duplicate_summary_rejected(self):
        self.assertFalse(verify_summary('Сектор C1 ряд 1 место 14 ряд 1 место 14 Итого: 15 000 ₽',self.seats,1500000))

    def test_summary_wrong_total(self):
        self.assertFalse(verify_summary('Сектор C1 ряд 1 место 14 Итого: 15 500 ₽',self.seats,2000000))

    def test_pay_is_not_navigation(self):
        self.assertFalse(permitted_label('Оплатить'))
        self.assertTrue(permitted_label('Перейти к оплате'))

    def test_journal_claim_durable(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'state.json'
            s=State(path);s.claim(self.seats)
            loaded=State(path)
            with self.assertRaises(RuntimeError):loaded.claim(self.seats)

    def test_corrupt_journal_stops(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'state.json';path.write_text('{bad')
            with self.assertRaises(RuntimeError):State(path)

    def test_redaction(self):
        raw={'email':'private@example.test','jwt':'secret','phone':'+70000000000','id':'private-order','statusCode':'order-validation-failed'}
        result=json.dumps(audit_shape(raw))
        for secret in ('private@example.test','secret','+70000000000','private-order'):
            self.assertNotIn(secret,result)
        self.assertIn('order-validation-failed',result)
