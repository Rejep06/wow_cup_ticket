import unittest
from core import extract_seats
from order_state import *
from tests.test_core import fixture

BASE='https://widget.afisha.yandex.ru/api/tickets/v2/orders/'
ORDER_ID='test-order-id'

def create_response():
    return {'status':'success','result':{'order':{'id':ORDER_ID,'expirationSeconds':899,'approved':False,'booked':False},'existingOrder':False}}

def reserve(status, code=''):
    return {'status':'success','result':{'operation':{'id':ORDER_ID,'status':status,'statusCode':code,'type':'reserve','expirationSeconds':895}}}

class OrderTests(unittest.TestCase):
    def tracker(self):
        t=OrderTracker();t.sent(BASE+'create','POST');t.observe(BASE+'create',create_response());return t

    def test_created_not_reserved(self):
        t=self.tracker();self.assertEqual(t.stage,'ORDER_CREATED');self.assertFalse(t.reserve_complete)

    def test_creation_without_request_ignored(self):
        t=OrderTracker();t.observe(BASE+'create',create_response());self.assertFalse(t.order_id)

    def test_user_actual_failed_trace(self):
        t=self.tracker()
        for _ in range(6):t.observe(BASE+ORDER_ID+'/operation/reserve',reserve('running'))
        self.assertEqual(t.stage,'RESERVING')
        t.observe(BASE+ORDER_ID+'/operation/reserve',reserve('failed','order-validation-failed'))
        self.assertEqual(t.stage,'RESERVE_FAILED');self.assertEqual(t.status_code,'order-validation-failed')
        self.assertFalse(t.reserve_complete)
        t.observe(BASE+ORDER_ID+'/cancel',{'status':'success'})
        self.assertEqual(t.stage,'CANCELLED');self.assertTrue(t.cancel_confirmed)

    def test_late_running_does_not_undo_failure(self):
        t=self.tracker();u=BASE+ORDER_ID+'/operation/reserve'
        t.observe(u,reserve('failed','order-validation-failed'));t.observe(u,reserve('running'))
        self.assertEqual(t.stage,'RESERVE_FAILED')

    def test_other_order_ignored(self):
        t=self.tracker();t.observe(BASE+'other/operation/reserve',reserve('success'))
        self.assertEqual(t.stage,'ORDER_CREATED')

    def test_unknown_status_not_success(self):
        t=self.tracker();t.observe(BASE+ORDER_ID+'/operation/reserve',reserve('purple'))
        self.assertEqual(t.stage,'UNKNOWN_RESERVE_STATUS');self.assertFalse(t.reserve_complete)

    def test_success_status_still_not_payment_ui(self):
        t=self.tracker();t.observe(BASE+ORDER_ID+'/operation/reserve',reserve('completed'))
        self.assertEqual(t.stage,'RESERVE_COMPLETED_REPORTED');self.assertFalse(t.order_verified)

    def test_preview_processing_not_order(self):
        t=OrderTracker();t.observe('https://widget.afisha.yandex.ru/api/session-order-preview/id',{'status':'success','orderPreviewInit':{'id':'x','status':'PROCESSING'}})
        self.assertEqual(t.stage,'STARTED')

    def test_existing_order_requires_review(self):
        t=OrderTracker();t.sent(BASE+'create','POST');data=create_response();data['result']['existingOrder']=True
        t.observe(BASE+'create',data);self.assertEqual(t.stage,'EXISTING_ORDER_REVIEW')

    def test_duplicate_create_not_safe_retry(self):
        t=self.tracker();t.sent(BASE+'create','POST')
        self.assertTrue(t.duplicate_request);self.assertEqual(t.stage,'MANUAL_REVIEW')

    def test_verify_complete_order(self):
        seats=extract_seats(fixture())
        order={'session':{'event':{'id':'908192'},'sessionDate':'2026-12-05T17:30+03:00'},
               'items':[{'type':'seat-ticket','sourceSeatId':'seat-test-14','hallplanLevelId':5,'row':'1','place':'14'}],
               'totalTicketCount':1, 'sum':{'total':{'currencyCode':'rub','value':1500000}}}
        self.assertTrue(verify_order(order,seats,908192,'2026-12-05',1500000))
        order['sum']['total']['value']=1550000
        self.assertFalse(verify_order(order,seats,908192,'2026-12-05',1600000))

    def test_only_matching_payment_way(self):
        self.assertEqual(collect_payment_ways({'paymentWay':'new-sbp-token'}),{'new-sbp-token'})
        self.assertEqual(collect_payment_ways({'paymentWay':'new-sbp-token','nested':{'paymentWay':'card'}}),{'new-sbp-token','card'})
