"""Regression tests for the requested 1..2 seats, non-adjacent and RUB 100k cap."""
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import os
import unittest

from core import choose_seats, rub_to_minor
from runtime import Config
from tests.test_core import fixture
from core import extract_seats

LIMIT = rub_to_minor('100000')


class UpToTwoTests(unittest.TestCase):
    def setUp(self):
        self.a = extract_seats(fixture())[0]

    def pick(self, seats, **overrides):
        options = dict(quantity=2, per_ticket=LIMIT, total_limit=LIMIT,
                       adjacent=False, min_quantity=1)
        options.update(overrides)
        return choose_seats(seats, **options)

    def test_one_available_not_skipped(self):
        self.assertEqual(self.pick([self.a]), [self.a])

    def test_two_non_adjacent_sectors_preferred(self):
        b = replace(self.a, source_id='new-40', level_id=6, level='Сектор C2',
                    row='5', place='75', total=rub_to_minor('80000'))
        self.assertEqual(len(self.pick([self.a, b])), 2)

    def test_one_if_event_maximum_is_one(self):
        self.assertEqual(len(self.pick([self.a], max_allowed=1)), 1)
        self.assertEqual(self.pick([self.a], max_allowed=0), [])

    def test_one_even_if_pair_exceeds_budget(self):
        a = replace(self.a, total=rub_to_minor('65000'))
        b = replace(self.a, source_id='new-2', total=rub_to_minor('65000'), place='99')
        selected = self.pick([a, b])
        self.assertEqual(len(selected), 1)
        self.assertLessEqual(sum(s.total for s in selected), LIMIT)

    def test_prefers_two_over_cheaper_single(self):
        a = replace(self.a, total=rub_to_minor('1000'))
        b = replace(self.a, source_id='another', total=rub_to_minor('99000'), level='Сектор C2')
        self.assertEqual(len(self.pick([a, b])), 2)

    def test_best_affordable_pair(self):
        a = replace(self.a, total=rub_to_minor('60000'))
        b = replace(self.a, source_id='b', total=rub_to_minor('80000'))
        c = replace(self.a, source_id='c', total=rub_to_minor('40000'))
        picked = self.pick([a, b, c])
        self.assertEqual(len(picked), 2)
        self.assertEqual(sum(s.total for s in picked), LIMIT)
        self.assertEqual({x.source_id for x in picked}, {a.source_id, c.source_id})

    def test_does_not_join_two_incompatible_sellers(self):
        b = replace(self.a, source_id='b', gateway_id='another-service', place='5')
        self.assertEqual(len(self.pick([self.a, b])), 1)

    def test_per_ticket_limit(self):
        expensive = replace(self.a, source_id='more', total=rub_to_minor('90000'))
        self.assertEqual(len(self.pick([self.a, expensive], per_ticket=rub_to_minor('20000'))), 1)

    def test_zero_or_invalid_prevented(self):
        self.assertEqual(self.pick([self.a], total_limit=0), [])
        self.assertEqual(self.pick([self.a], min_quantity=3), [])

    def test_legacy_exact_count_is_unchanged(self):
        self.assertEqual(choose_seats([self.a], 2, LIMIT, LIMIT, adjacent=False), [])

    def test_config_values(self):
        values = {'BOOKING_SEATS':'2', 'BOOKING_MIN_SEATS':'1',
                  'BOOKING_MAX_PRICE_RUB':'100000', 'BOOKING_MAX_TOTAL_RUB':'100000',
                  'BOOKING_ADJACENT':'false', 'BOOKING_SECTORS':'',
                  'AUTO_BOOKING':'true', 'AUTO_START_SBP':'true', 'BOOKING_HEADLESS':'false',
                  'BUYER_FULL_NAME':'Test Buyer', 'BUYER_EMAIL':'test@example.test',
                  'BUYER_PHONE':'+70000000000'}
        with patch.dict(os.environ, values, clear=True):
            cfg = Config.load(Path('/tmp'))
            self.assertEqual((cfg.min_quantity, cfg.quantity, cfg.adjacent), (1, 2, False))
            self.assertEqual((cfg.max_price, cfg.max_total), (LIMIT, LIMIT))
            cfg.validate_automation()

    def test_config_rejects_bad_min(self):
        with patch.dict(os.environ, {'BOOKING_SEATS':'1','BOOKING_MIN_SEATS':'2'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'BOOKING_MIN_SEATS'):
                Config.load(Path('/tmp'))


if __name__ == '__main__':
    unittest.main()
