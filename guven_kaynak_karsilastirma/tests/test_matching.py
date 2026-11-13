import datetime

from odoo.tests import BaseCase, tagged

from ..services import cerebrum_client, matching as m, pusula_client

D = datetime.date(2026, 10, 15)


def rec(number="GVN2026000000001", date=D, direction=m.OUT, unit=1, valid=True, system=m.PUSULA,
        amount=100.0, identity=None, ettn=None, rid=1):
    return m.SourceRecord(system=system, record_id=rid, numbers=(number,), date=date, direction=direction,
                          type_id=8, unit_id=unit, amount_try=amount, identity=identity, ettn=ettn,
                          invalid_reason=None if valid else "cancelled")


def inv(number="GVN2026000000001", date=D, direction=m.OUT, amount=100.0, identity=None, ettn=None, payable=None):
    return m.GibInvoice(invoice_id=1, company_id=2, direction=direction, number=number, date=date,
                        amount_try=amount, counterparty_identity=identity, ettn=ettn, payable_amount_try=payable)


@tagged('post_install', '-at_install', 'guven_kaynak')
class TestVerdictLadder(BaseCase):

    def test_single_valid_same_date_is_found(self):
        v = m.judge(inv(), m.build_index([rec()]), {1})
        self.assertEqual((v.status, v.candidate_count), (m.FOUND, 1))

    def test_two_valid_same_date_is_multiple(self):
        v = m.judge(inv(), m.build_index([rec(rid=1), rec(rid=2)]), {1})
        self.assertEqual(v.status, m.MULTIPLE)
        self.assertIsNone(v.record)
        self.assertEqual(len(v.candidates), 2)

    def test_valid_other_date_is_date_mismatch_with_nearest(self):
        far = rec(date=D - datetime.timedelta(days=20), rid=1)
        near = rec(date=D + datetime.timedelta(days=2), rid=2)
        v = m.judge(inv(), m.build_index([far, near]), {1})
        self.assertEqual((v.status, v.record.record_id), (m.DATE_MISMATCH, 2))

    def test_valid_other_date_beats_invalid_same_date(self):
        v = m.judge(inv(), m.build_index([rec(valid=False, rid=1), rec(date=D + datetime.timedelta(days=1), rid=2)]), {1})
        self.assertEqual(v.status, m.DATE_MISMATCH)

    def test_only_invalid_prefers_same_date(self):
        same = rec(valid=False, rid=1)
        other = rec(valid=False, date=D - datetime.timedelta(days=3), rid=2)
        v = m.judge(inv(), m.build_index([other, same]), {1})
        self.assertEqual((v.status, v.record.record_id), (m.INVALID, 1))

    def test_no_candidate_is_missing(self):
        self.assertEqual(m.judge(inv(), {}, {1}).status, m.MISSING)


@tagged('post_install', '-at_install', 'guven_kaynak')
class TestScopeAndValidators(BaseCase):

    def test_direction_filter(self):
        v = m.judge(inv(direction=m.OUT), m.build_index([rec(direction=m.IN)]), {1})
        self.assertEqual(v.status, m.MISSING)
        self.assertFalse(v.found_in_other_company)

    def test_other_company_is_diagnostic_only(self):
        v = m.judge(inv(), m.build_index([rec(unit=7)]), {1})
        self.assertEqual((v.status, v.found_in_other_company, v.candidate_count), (m.MISSING, True, 0))

    def test_number_normalisation(self):
        v = m.judge(inv(number="  gvn2026000000001 "), m.build_index([rec()]), {1})
        self.assertEqual(v.status, m.FOUND)

    def test_amount_levels(self):
        self.assertEqual(m.amount_level(m.judge(inv(), m.build_index([rec(amount=100.015)]), {1})), m.AMOUNT_EXACT)
        self.assertEqual(m.amount_level(m.judge(inv(), m.build_index([rec(amount=100.6)]), {1})), m.AMOUNT_ROUNDING)
        v = m.judge(inv(), m.build_index([rec(amount=150.0)]), {1})
        self.assertEqual((m.amount_level(v), v.amount_diff), (m.AMOUNT_DIFFERENT, 50.0))

    def test_payable_basis_for_withholding(self):
        v = m.judge(inv(direction=m.IN, amount=1180.0, payable=1000.0),
                    m.build_index([rec(direction=m.IN, amount=1000.0)]), {1})
        self.assertEqual((v.amount_basis, m.amount_level(v)), ('payable', m.AMOUNT_EXACT))

    def test_identity_only_for_incoming(self):
        self.assertIsNone(m.judge(inv(identity="1234567890"), m.build_index([rec(identity="999")]), {1}).identity_match)
        v = m.judge(inv(direction=m.IN, identity="123 456 7890"),
                    m.build_index([rec(direction=m.IN, identity="1234567890")]), {1})
        self.assertTrue(v.identity_match)

    def test_ettn_only_for_pusula_outgoing_and_never_changes_verdict(self):
        v = m.judge(inv(ettn="abc-1"), m.build_index([rec(ettn="ABC-2")]), {1})
        self.assertEqual((v.status, v.ettn_match), (m.FOUND, False))
        self.assertIsNone(m.judge(inv(ettn="a"), m.build_index([rec(ettn="b", system=m.CEREBRUM)]), {1}).ettn_match)

    def test_overall_status(self):
        def v(status):
            return m.SourceVerdict(status=status)
        self.assertEqual(m.overall_status(v(m.FOUND), v(m.FOUND)), m.BOTH)
        self.assertEqual(m.overall_status(v(m.FOUND), v(m.MISSING)), m.PUSULA)
        self.assertEqual(m.overall_status(v(m.INVALID), v(m.FOUND)), m.CEREBRUM)
        self.assertEqual(m.overall_status(v(m.INVALID), v(m.MULTIPLE)), m.MULTIPLE)
        self.assertEqual(m.overall_status(v(m.MISSING), v(m.MISSING)), m.MISSING)


@tagged('post_install', '-at_install', 'guven_kaynak')
class TestRowMapping(BaseCase):

    def test_pusula_rows(self):
        client = pusula_client.PusulaClient("h", 1, "d", "u", "p")
        tl = (1, None, None, None, None)  # ParaBirimiId, KDV-hariç, KDV, tevkifat pay/payda
        self.assertEqual(client._to_record((10, 13, 1, 1, 1, "GVN1", D, 118.0, None, "u-1") + tl).direction, m.OUT)
        self.assertIsNone(client._to_record((11, 9, 1, 1, 1, "GVN1", D, 1.0, None, "u") + tl))
        self.assertIsNone(client._to_record((12, 99, 1, 1, 1, "X", D, 1.0, None, None) + tl))
        self.assertEqual(client.unknown_types[99], 1)
        self.assertEqual(client._to_record((13, 8, 1, 2, 1, "X", D, 1.0, None, None) + tl).invalid_reason, "merged")
        self.assertEqual(client._to_record((14, 4, 2, 1, 1, "X", D, 1.0, None, None) + tl).invalid_reason, "receipt")
        self.assertTrue(client._to_record((15, 4, 4, 1, 1, "X", D, 1.0, None, None) + tl).is_valid)

    def _row(self, rid, type_id, amount, currency_id=1, net=None, vat=None, num=None, den=None):
        return (rid, type_id, 1, 1, 1, "G%s" % rid, D, amount, None, "u", currency_id, net, vat, num, den)

    def test_pusula_foreign_currency_uses_line_totals(self):
        client = pusula_client.PusulaClient("h", 1, "d", "u", "p")
        # Type 8, real case G512026000169656: header 36,282.93 is USD, the
        # lines carry TL 1,774,264.17 (GİB 1,774,264.30 TL).
        usd = client._to_record(self._row(20, 8, 36282.93, 2), lines=(1774264.17, 36282.93, 1145))
        self.assertEqual((usd.currency, usd.amount_currency, usd.amount_try), ('USD', 36282.93, 1774264.17))
        tl = client._to_record(self._row(22, 8, 118.0))
        self.assertEqual((tl.currency, tl.amount_currency, tl.amount_try), (None, None, 118.0))
        self.assertEqual(client._to_record(self._row(23, 8, 10.0, 99)).currency, 'PB99')

    def test_pusula_institution_invoice_uses_children_lines(self):
        client = pusula_client.PusulaClient("h", 1, "d", "u", "p")
        # Real case G022026000052192: header 14,000 (one provision), 38 children total 472,000.
        tl = client._to_record(self._row(30, 13, 14000.0), children=(472000.0, 0.0, 38))
        self.assertEqual((tl.amount_try, tl.currency), (472000.0, None))
        # Foreign-currency type 13: children lines carry TL and currency.
        eur = client._to_record(self._row(31, 13, 2566562.35, 3), children=(2566562.11, 46001.59, 12))
        self.assertEqual((eur.amount_try, eur.currency, eur.amount_currency), (2566562.11, 'EUR', 46001.59))
        # Own lines are ignored for type 13; no children keeps the header.
        alone = client._to_record(self._row(32, 13, 500.0, 2), lines=(1.0, 1.0, 1), children=None)
        self.assertEqual((alone.amount_try, alone.amount_currency), (500.0, None))

    def test_pusula_withholding_gives_payable_amount(self):
        client = pusula_client.PusulaClient("h", 1, "d", "u", "p")
        # Real case GNS2026000003192: 99.21 + 9.92 * (1 - 5/10) = 104.17, GİB 104.17.
        ret = client._to_record(self._row(40, 4, 109.13, 1, 99.21, 9.92, 5.0, 10.0))
        self.assertEqual((ret.amount_try, ret.amount_payable_try), (109.13, 104.17))
        self.assertIsNone(client._to_record(self._row(41, 4, 109.13)).amount_payable_try)
        # Header amounts are not TL on foreign-currency invoices: no payable.
        self.assertIsNone(client._to_record(self._row(42, 4, 10.0, 2, 9.0, 1.0, 5.0, 10.0)).amount_payable_try)

    def test_withheld_payable_matches_gib(self):
        record = m.SourceRecord(system=m.PUSULA, record_id=1, numbers=("GNS1",), date=D, direction=m.IN, type_id=4,
                                unit_id=1, amount_try=109.13, amount_payable_try=104.17)
        v = m.judge(inv(number="GNS1", direction=m.IN, amount=104.17, payable=104.17), m.build_index([record]), {1})
        self.assertEqual((m.amount_level(v), v.amount_basis, v.amount_diff), (m.AMOUNT_EXACT, 'payable', 0.0))
        # Equal GİB totals keep the inclusive basis, so existing signatures stay put.
        plain = m.SourceRecord(system=m.PUSULA, record_id=2, numbers=("X1",), date=D, direction=m.OUT, type_id=8,
                               unit_id=1, amount_try=118.0)
        v = m.judge(inv(number="X1", amount=118.0, payable=118.0), m.build_index([plain]), {1})
        self.assertEqual(v.amount_basis, 'inclusive')

    def test_cerebrum_rows(self):
        to = cerebrum_client.CerebrumClient._to_record
        incoming = to((1, 27, 0, 1, "ABC2026000000001", None, datetime.datetime(2026, 10, 15), 50, "123"))
        self.assertEqual((incoming.direction, incoming.numbers, incoming.date), (m.IN, ("ABC2026000000001",), D))
        outgoing = to((2, 25, 2, 7, "INT-1", "GVN2026000000009", datetime.datetime(2026, 10, 15), 50, None))
        self.assertEqual((outgoing.numbers, outgoing.invalid_reason), (("GVN2026000000009", "INT-1"), "incomplete"))
