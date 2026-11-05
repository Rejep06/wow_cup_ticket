"""All tests are offline; run: python -m unittest -v"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import watcher as w


NUXT_EXAMPLE = r'''<html><head><script>window.__NUXT__={};window.__NUXT__.config={public:{ticketUrl:"https:\/\/widget.afisha.yandex.ru\/w\/sessions\/ticketsteam-8261@77813737?clientKey=5a2f3e1d-f923-4b55-8a34-636ba6dfe922&regionId=213",streamUrl:""}}</script></head></html>'''
NEW_URL = "https://widget.afisha.yandex.ru/w/sessions/ticketsteam-8261@77813737?clientKey=5a2f3e1d-f923-4b55-8a34-636ba6dfe922&regionId=213"
OLD_URL = "https://widget.afisha.yandex.ru/w/sessions/MTEwOTYzfDkwODE5MnwxNDk2OTE2MnwxNzk2NDc3NDAwMDAw?clientKey=5a2f3e1d-f923-4b55-8a34-636ba6dfe922"


def data(count=0, event_id=908192, date="2026-12-05T17:30+03:00", session_id=143823287):
    return {
        "status": "success",
        "result": {"session": {
            "id": session_id,
            "key": "MTEwOTYzfDkwODE5MnwxNDk2OTE2MnwxNzk2NDgxMDAwMDAw",  # canonical != alias
            "availableSeatCount": count,
            "saleStatus": "available" if count else "no-seats",
            "sessionDate": date,
            "event": {"id": event_id, "name": "WOW CUP: Медведев vs Рублёв", "saleStatus": "available"},
            "maxTicketsCount": 2,
        }}
    }


def http_response(*, json_data=None, text=None, error=None):
    res = Mock()
    res.raise_for_status.side_effect = error
    res.json.return_value = json_data
    if text is not None:
        res.text = text
        res.content = text.encode('utf-8')
    return res


class TicketUrlTests(unittest.TestCase):
    def test_extract_inline_nuxt_config(self):
        self.assertEqual(w.extract_ticket_url(NUXT_EXAMPLE), NEW_URL)

    def test_extract_json_quoted_key_and_unicode_ampersand(self):
        src = r'''<script>window.__NUXT__.config={"ticketUrl":"https:\/\/widget.afisha.yandex.ru\/w\/sessions\/ticketsteam-8261@77813737?clientKey=abc\u0026regionId=213"}</script>'''
        self.assertEqual(w.extract_ticket_url(src), "https://widget.afisha.yandex.ru/w/sessions/ticketsteam-8261@77813737?clientKey=abc&regionId=213")

    def test_extract_html_anchor_with_html_entities(self):
        doc = '<a href="https://widget.afisha.yandex.ru/w/sessions/ticketsteam-8261@77813737?clientKey=abc&amp;regionId=213">Buy</a>'
        self.assertEqual(w.extract_ticket_url(doc), "https://widget.afisha.yandex.ru/w/sessions/ticketsteam-8261@77813737?clientKey=abc&regionId=213")

    def test_does_not_follow_unsafe_host(self):
        for url in ("https://evil.invalid/w/sessions/foo?clientKey=x", "http://widget.afisha.yandex.ru/w/sessions/foo?clientKey=x", "https://widget.afisha.yandex.ru.evil.invalid/w/sessions/foo?clientKey=x", "https://widget.afisha.yandex.ru/w/sessions/../x?clientKey=x"):
            with self.subTest(url=url), self.assertRaises(w.DiscoveryError):
                w.target_from_url(url)

    def test_api_url_derived_from_session_alias(self):
        target = w.target_from_url(NEW_URL)
        self.assertEqual(target.alias, "ticketsteam-8261@77813737")
        self.assertEqual(target.api_url, "https://widget.afisha.yandex.ru/api/tickets/v1/sessions/ticketsteam-8261@77813737?clientKey=5a2f3e1d-f923-4b55-8a34-636ba6dfe922&req_number=0")

    def test_discover_from_website(self):
        http = Mock()
        http.get.return_value = http_response(text=NUXT_EXAMPLE)
        target = w.discover_ticket_target(http)
        self.assertEqual(target.widget_url, NEW_URL)
        http.get.assert_called_once()

    def test_fallback_external_same_origin_js(self):
        doc = '<script src="/_nuxt/start.js"></script><script src="https://evil.invalid/app.js"></script>'
        http = Mock()
        http.get.side_effect = [http_response(text=doc), http_response(text=NUXT_EXAMPLE)]
        target = w.discover_ticket_target(http)
        self.assertEqual(target.widget_url, NEW_URL)
        self.assertEqual(http.get.call_count, 2)
        self.assertEqual(http.get.call_args.args[0], 'https://wowcup.tennis/_nuxt/start.js')

    def test_invalid_site_config_is_reported(self):
        with self.assertRaises(w.DiscoveryError):
            w.extract_ticket_url('<script>window.__NUXT__.config={public:{ticketUrl:"https://evil.invalid/pay"}}</script>')


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.target = w.target_from_url(NEW_URL)

    def test_canonical_key_not_equal_alias_is_valid(self):
        snap = w.parse_snapshot(data(), self.target)
        self.assertEqual(snap.session_id, "143823287")
        self.assertEqual(snap.count, 0)
        self.assertEqual(snap.status, "no-seats")
        self.assertEqual(snap.max_tickets, 2)

    def test_event_available_does_not_trigger_when_session_zero(self):
        state = {"count": 0}
        snap = w.parse_snapshot(data(), self.target)
        self.assertIsNone(w.alert_reason(state, snap, now=1000))
        with patch.object(w, "send_message") as send:
            w.process_snapshot(snap, state, now=1000)
        send.assert_not_called()

    def test_1_or_2_tickets_trigger(self):
        for count in (1, 2):
            with self.subTest(count=count):
                self.assertEqual(w.alert_reason({"count": 0}, w.parse_snapshot(data(count=count), self.target), 1000), "appeared")

    def test_schema_changed_fails_instead_of_reporting_zero(self):
        modified = data()
        del modified["result"]["session"]["availableSeatCount"]
        with self.assertRaisesRegex(w.SessionDataError, "availableSeatCount"):
            w.parse_snapshot(modified, self.target)

    def test_wrong_event_and_date_fail(self):
        with self.assertRaisesRegex(w.SessionDataError, "Wrong event"):
            w.parse_snapshot(data(event_id=777), self.target)
        with self.assertRaisesRegex(w.SessionDataError, "Wrong session date"):
            w.parse_snapshot(data(date="2027-12-05T17:30+03:00"), self.target)

    def test_fetch_failure_bad_json(self):
        resp = http_response()
        resp.json.side_effect = ValueError('not json')
        http = Mock()
        http.get.return_value = resp
        with self.assertRaises(w.SessionDataError):
            w.fetch_snapshot(http, self.target)

    def test_fetch_uses_current_api_url(self):
        http = Mock()
        http.get.return_value = http_response(json_data=data())
        snap = w.fetch_snapshot(http, self.target)
        self.assertEqual(snap.widget_url, NEW_URL)
        self.assertEqual(http.get.call_args.args[0], self.target.api_url)


class MonitoringTests(unittest.TestCase):
    def test_page_discovery_updates_cached_session_and_clears_old_count(self):
        state = {'ticket_url': OLD_URL, 'session_id': '143440234', 'count': 5}
        with patch.object(w, 'TELEGRAM_CHAT_IDS', ('123',)):
            changed = w.set_target(state, w.target_from_url(NEW_URL))
        self.assertTrue(changed)
        self.assertEqual(state['ticket_url'], NEW_URL)
        self.assertIsNone(state['count'])
        self.assertEqual(state['pending_url_notification'], NEW_URL)

    def test_404_refreshes_landing_and_retries_changed_api(self):
        old = w.target_from_url(OLD_URL)
        new = w.target_from_url(NEW_URL)
        state = {'ticket_url': OLD_URL, 'last_discovery_attempt_at': 1000}
        http = Mock()
        # initial discovery is recent; first request is the old API (404),
        # next is landing HTML (new target), next is new API (success).
        resp_404 = http_response()
        response = requests.Response()
        response.status_code = 404
        response.url = old.api_url
        resp_404.raise_for_status.side_effect = requests.HTTPError('404', response=response)
        http.get.side_effect = [resp_404, http_response(text=NUXT_EXAMPLE), http_response(json_data=data())]
        with patch.object(w, 'send_message'):
            snap = w.poll(state, http, now=1020)
        self.assertEqual(snap.count, 0)
        self.assertEqual(snap.widget_url, NEW_URL)
        self.assertEqual(http.get.call_count, 3)
        self.assertEqual(http.get.call_args.args[0], new.api_url)

    def test_404_same_link_does_not_retry_old_api_immediately(self):
        state = {'ticket_url': NEW_URL, 'last_discovery_attempt_at': 1000}
        response = requests.Response()
        response.status_code = 404
        response.url = w.target_from_url(NEW_URL).api_url
        error = requests.HTTPError('404', response=response)
        resp_404 = http_response(error=error)
        http = Mock()
        http.get.side_effect = [resp_404, http_response(text=NUXT_EXAMPLE)]
        with patch.object(w, 'send_message'):
            with self.assertRaises(requests.HTTPError):
                w.poll(state, http, now=1020)
        self.assertEqual(http.get.call_count, 2)
        self.assertEqual(state['ticket_url'], NEW_URL)

    def test_429_does_not_force_discovery_again(self):
        state = {'ticket_url': NEW_URL, 'last_discovery_attempt_at': 1000}
        response = requests.Response()
        response.status_code = 429
        response.headers['Retry-After'] = '300'
        error = requests.HTTPError('429', response=response)
        http = Mock()
        http.get.return_value = http_response(error=error)
        with self.assertRaises(requests.HTTPError):
            w.poll(state, http, now=1020)
        http.get.assert_called_once()
        self.assertEqual(w.server_retry_after(error), 300)

    def test_discovery_fails_cached_url_still_works(self):
        state = {'ticket_url': NEW_URL}
        http = Mock()
        http.get.side_effect = [requests.Timeout('site unavailable'), http_response(json_data=data())]
        with patch.object(w, 'send_message'):
            snap = w.poll(state, http, now=1000)
        self.assertEqual(snap.count, 0)
        self.assertEqual(http.get.call_count, 2)
        self.assertIn('site unavailable', state['last_discovery_error'])

    def test_backoff_and_reset(self):
        with patch.object(w, 'CHECK_INTERVAL_SECONDS', 60.0), patch.object(w, 'MAX_ERROR_INTERVAL_SECONDS', 3600.0):
            self.assertEqual([w.next_check_delay(i) for i in range(10)], [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600, 3600])
            state = {'count': 0, 'consecutive_errors': 100}
            with patch.object(w, 'send_message'):
                w.process_snapshot(w.parse_snapshot(data(), w.target_from_url(NEW_URL)), state, 1000)
            self.assertEqual(w.next_check_delay(state['consecutive_errors']), 60)

    def test_ticket_message_links_new_widget(self):
        snap = w.parse_snapshot(data(count=1), w.target_from_url(NEW_URL))
        message = w.ticket_message(snap, 'appeared')
        self.assertIn('ticketsteam-8261@77813737', message)
        self.assertNotIn('MTEwOTYzfDkwODE5MnwxNDk2OTE2MnwxNzk2NDc3NDAwMDAw', message)

    def test_multi_chat_send_continues_after_failure_and_retries_only_failed(self):
        sent = []
        def delivery(method, payload):
            sent.append(payload['chat_id'])
            if payload['chat_id'] == '456':
                raise RuntimeError('blocked')
        with patch.object(w, 'telegram_call', side_effect=delivery):
            with self.assertRaises(w.TelegramDeliveryError) as raised:
                w.send_message('hello', chat_ids=('123', '456', '789'))
        self.assertEqual(sent, ['123', '456', '789'])
        self.assertEqual(raised.exception.failed_chat_ids, ['456'])
        state = {'count': 0}
        snap = w.parse_snapshot(data(count=1), w.target_from_url(NEW_URL))
        with patch.object(w, 'send_message', side_effect=w.TelegramDeliveryError(['456'])):
            w.process_snapshot(snap, state, now=1000)
        with patch.object(w, 'send_message') as send:
            w.process_snapshot(snap, state, now=1050)
        self.assertEqual(send.call_args.kwargs['chat_ids'], ['456'])
        self.assertFalse(state['pending_ticket_alert'])

    def test_changed_link_notice_not_mistaken_for_available_tickets(self):
        state = {'pending_url_notification': NEW_URL, 'pending_url_chat_ids': ['123']}
        with patch.object(w, 'send_message') as send:
            w.notify_url_change(state)
        text = send.call_args.args[0]
        self.assertIn('Наличие билетов пока не подтверждено', text)
        self.assertNotIn('Билеты появились', text)
        self.assertIsNone(state['pending_url_notification'])

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'data' / 'state.json'
            state = {'ticket_url': NEW_URL, 'consecutive_errors': 6}
            w.save_state(state, path)
            self.assertEqual(w.load_state(path), state)

    def test_parse_multiple_chat_ids(self):
        self.assertEqual(w.parse_chat_ids('1, 2, 1, -10000002'), ('1', '2', '-10000002'))


if __name__ == '__main__':
    unittest.main()
