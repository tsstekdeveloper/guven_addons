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
        self.assertEqual(client._to_record((10, 13, 1, 1, 1, "GVN1", D, 118.0, None, "u-1")).direction, m.OUT)
        self.assertIsNone(client._to_record((11, 9, 1, 1, 1, "GVN1", D, 1.0, None, "u")))
        self.assertIsNone(client._to_record((12, 99, 1, 1, 1, "X", D, 1.0, None, None)))
        self.assertEqual(client.unknown_types[99], 1)
        self.assertEqual(client._to_record((13, 8, 1, 2, 1, "X", D, 1.0, None, None)).invalid_reason, "merged")
        self.assertEqual(client._to_record((14, 4, 2, 1, 1, "X", D, 1.0, None, None)).invalid_reason, "receipt")
        self.assertTrue(client._to_record((15, 4, 4, 1, 1, "X", D, 1.0, None, None)).is_valid)

    def test_cerebrum_rows(self):
        to = cerebrum_client.CerebrumClient._to_record
        incoming = to((1, 27, 0, 1, "ABC2026000000001", None, datetime.datetime(2026, 10, 15), 50, "123"))
        self.assertEqual((incoming.direction, incoming.numbers, incoming.date), (m.IN, ("ABC2026000000001",), D))
        outgoing = to((2, 25, 2, 7, "INT-1", "GVN2026000000009", datetime.datetime(2026, 10, 15), 50, None))
        self.assertEqual((outgoing.numbers, outgoing.invalid_reason), (("GVN2026000000009", "INT-1"), "incomplete"))
