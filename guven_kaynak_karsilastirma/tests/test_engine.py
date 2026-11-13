"""Tour engine tests against fake Pusula and Cerebrum clients.

All invoices live in 2030 and the start date is moved there, so the tour
never touches real guven.fatura data of the database the tests run on.
"""
import collections
import datetime
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from ..models.kaynak_karsilastirma import FULL_SCAN_DATE_PARAM, START_DATE_PARAM
from ..services import matching as m

DAY = datetime.date(2030, 1, 10)
TODAY = datetime.date(2030, 1, 20)


class FakePusula:
    def __init__(self, records=(), fail=False):
        self.records = list(records)
        self.fail = fail
        self.unknown_types = collections.Counter()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def fetch_by_numbers(self, numbers):
        if self.fail:
            raise RuntimeError("pusula down")
        wanted = {m.normalize_number(n) for n in numbers}
        return [r for r in self.records if {m.normalize_number(n) for n in r.numbers} & wanted]


class FakeCerebrum:
    def __init__(self, records=(), fail=False):
        self.records = list(records)
        self.fail = fail

    def __enter__(self):
        if self.fail:
            raise RuntimeError("cerebrum down")
        return self

    def __exit__(self, *exc_info):
        return False

    def fetch_since(self, date_from):
        return list(self.records)


def source_record(system, number, rid, direction=m.OUT, amount=118.0, unit=1, valid=True, date=DAY, ettn=None,
                  identity=None, currency=None, amount_currency=None):
    return m.SourceRecord(
        system=system, record_id=rid, numbers=(number,), date=date, direction=direction,
        type_id=8 if system == m.PUSULA else 27, unit_id=unit, amount_try=amount, identity=identity,
        ettn=ettn, invalid_reason=None if valid else 'cancelled',
        currency=currency, amount_currency=amount_currency,
    )


@tagged('post_install', '-at_install', 'guven_kaynak')
class TestTourEngine(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Comparison = cls.env['guven.kaynak.karsilastirma']
        cls.Source = cls.env['guven.kaynak.source']
        cls.company = cls.env.company
        companies = cls.env['res.company'].sudo().search([])
        companies.write({'kaynak_pusula_branch_ids': False, 'kaynak_cerebrum_company_ids': False})
        cls.company.sudo().write({'kaynak_pusula_branch_ids': '1', 'kaynak_cerebrum_company_ids': '1'})
        params = cls.env['ir.config_parameter'].sudo()
        params.set_param(START_DATE_PARAM, '2030-01-01')
        params.set_param(FULL_SCAN_DATE_PARAM, False)

        cls.out_invoice = cls._invoice('GKT2030000000001', 'OUT', 'e-arsiv-izibiz', uuid='ettn-out-1')
        cls.in_invoice = cls._invoice('TED2030000000001', 'IN', 'e-fatura-izibiz', amount=1180.0, payable=1000.0,
                                      sender='1234567890')
        cls.missing_invoice = cls._invoice('GKT2030000000002', 'OUT', 'e-fatura-izibiz')

        cls.pusula = FakePusula([
            source_record(m.PUSULA, 'GKT2030000000001', 101, ettn='ETTN-OUT-1'),
        ])
        cls.cerebrum = FakeCerebrum([
            source_record(m.CEREBRUM, 'TED2030000000001', 201, direction=m.IN, amount=1000.0, identity='1234567890'),
        ])

    @classmethod
    def _invoice(cls, number, direction, kaynak, amount=118.0, payable=None, uuid=None, sender=None):
        return cls.env['guven.fatura'].create({
            'invoice_id': number,
            'uuid': uuid or number.lower() + '-uuid',
            'kaynak': kaynak,
            'company_id': cls.company.id,
            'direction': direction,
            'issue_date': DAY,
            'currency_code': 'TRY',
            'tax_inclusive_amount': amount,
            'payable_amount': payable if payable is not None else amount,
            'sender': sender,
        })

    def _run(self, pusula=None, cerebrum=None):
        fakes = {'pusula': pusula or self.pusula, 'cerebrum': cerebrum or self.cerebrum}
        SourceClass = type(self.Source)
        with patch.object(SourceClass, 'has_credentials', autospec=True, return_value=True), \
                patch.object(SourceClass, '_client', autospec=True, side_effect=lambda src: fakes[src.system]):
            return self.Comparison._run_tour(commit=False, budget=60, today=TODAY)

    def _row(self, invoice):
        return self.Comparison.search([('fatura_id', '=', invoice.id)])

    def test_first_tour_builds_one_row_per_valid_invoice(self):
        stats = self._run()
        self.assertEqual(stats['created'], 3)

        out_row = self._row(self.out_invoice)
        self.assertEqual(out_row.overall_status, 'pusula')
        self.assertEqual(out_row.pusula_status, 'found')
        self.assertEqual(out_row.pusula_record_id, 101)
        self.assertEqual(out_row.pusula_ettn_match, 'match')
        self.assertEqual(out_row.pusula_amount_level, 'exact')
        self.assertEqual(out_row.cerebrum_status, 'missing')
        self.assertFalse(out_row.counterparty_vkn, "outgoing counterparty identity must not be copied")

        in_row = self._row(self.in_invoice)
        self.assertEqual(in_row.overall_status, 'cerebrum')
        self.assertEqual(in_row.cerebrum_amount_basis, 'payable')
        self.assertEqual(in_row.cerebrum_amount_level, 'exact')
        self.assertEqual(in_row.cerebrum_identity_match, 'match')
        self.assertEqual(in_row.counterparty_vkn, '1234567890')

        self.assertEqual(self._row(self.missing_invoice).overall_status, 'missing')

    def test_unchanged_tour_writes_nothing(self):
        self._run()
        stats = self._run()
        self.assertEqual((stats['created'], stats['updated'], stats['removed']), (0, 0, 0))

    def test_source_change_rewrites_only_that_part(self):
        self._run()
        row = self._row(self.out_invoice)
        cerebrum_signature = row.cerebrum_signature
        changed = FakePusula([source_record(m.PUSULA, 'GKT2030000000001', 101, amount=150.0, ettn='ETTN-OUT-1')])
        stats = self._run(pusula=changed)
        self.assertEqual(stats['updated'], 1)
        self.assertEqual(row.pusula_amount_level, 'different')
        self.assertEqual(row.pusula_amount_diff, 32.0)
        self.assertEqual(row.cerebrum_signature, cerebrum_signature)

    def test_pusula_failure_keeps_previous_result(self):
        self._run()
        late = self._invoice('GKT2030000000003', 'OUT', 'e-arsiv-izibiz')
        stats = self._run(pusula=FakePusula(fail=True))
        self.assertEqual(stats['source_errors'], 1)
        self.assertEqual(self._row(self.out_invoice).pusula_status, 'found')
        late_row = self._row(late)
        self.assertEqual(late_row.pusula_status, 'unchecked')
        self.assertEqual(late_row.overall_status, 'unchecked')
        pusula_source = self.Source._get('pusula')
        self.assertTrue(pusula_source.has_error)
        self.assertIn('pusula down', pusula_source.last_error)

        # The next healthy tour picks the unchecked row up again.
        self._run()
        self.assertEqual(late_row.pusula_status, 'missing')

    def test_cerebrum_shrink_guard(self):
        filler = [source_record(m.CEREBRUM, 'X%04d' % i, 1000 + i, direction=m.IN) for i in range(9)]
        self._run(cerebrum=FakeCerebrum(self.cerebrum.records + filler))
        self.assertEqual(self.Source._get('cerebrum').baseline_record_count, 10)
        stats = self._run(cerebrum=FakeCerebrum([]))
        self.assertEqual(stats['source_errors'], 1)
        self.assertEqual(self._row(self.in_invoice).cerebrum_status, 'found')
        self.assertTrue(self.Source._get('cerebrum').has_error)

    def test_invalidated_izibiz_invoice_is_removed(self):
        self._run()
        self.out_invoice.write({'harici_iptal': True})
        stats = self._run()
        self.assertEqual(stats['removed'], 1)
        self.assertFalse(self._row(self.out_invoice))

    def test_direction_and_company_scope(self):
        pusula = FakePusula([
            source_record(m.PUSULA, 'GKT2030000000002', 102, direction=m.IN),
            source_record(m.PUSULA, 'GKT2030000000001', 101, unit=7),
        ])
        self._run(pusula=pusula)
        self.assertEqual(self._row(self.missing_invoice).pusula_status, 'missing')
        out_row = self._row(self.out_invoice)
        self.assertEqual(out_row.pusula_status, 'missing')
        self.assertTrue(out_row.pusula_other_company)

    def test_foreign_currency_columns_follow_the_source(self):
        usd = FakePusula([source_record(m.PUSULA, 'GKT2030000000001', 101, ettn='ETTN-OUT-1',
                                        currency='USD', amount_currency=2.55)])
        self._run(pusula=usd)
        row = self._row(self.out_invoice)
        self.assertEqual((row.pusula_currency, row.pusula_amount_currency, row.pusula_amount_level),
                         ('USD', 2.55, 'exact'))
        # The same document turning into a TL record must clear the columns.
        self._run()
        self.assertEqual((row.pusula_currency, row.pusula_amount_currency), (False, 0.0))

    def test_full_scan_marked_done(self):
        self._run()
        self.assertEqual(self.env['ir.config_parameter'].sudo().get_param(FULL_SCAN_DATE_PARAM), TODAY.isoformat())
