"""Matching rules for izibiz invoices against Pusula and Cerebrum records.

The module has no Odoo imports on purpose: the rules are unit-testable on
their own and the same code runs in the dry run before the addon is
installed. Design: docs-reports/kaynak_karsilastirma_tasarim.md.

Verdict = normalised invoice number + exact invoice date, within the same
direction and the company's source scope. ETTN, amount and counterparty
identity are validators only; they never change the verdict.
"""
import dataclasses
import datetime
import re

IN = 'IN'
OUT = 'OUT'

PUSULA = 'pusula'
CEREBRUM = 'cerebrum'

# Pusula Finans.Fatura.FaturaTipiId -> direction. Names come from the
# official list shared by the user on 2026-11-12; the Pusula.FaturaTipi table
# in the database is stale (13 missing, 9 misnamed). Type 9 (invoice in
# provision state) is never read: its parent type 13 carries the same
# document with the correct date.
PUSULA_DIRECTION = {
    1: IN, 4: IN, 5: IN, 7: IN,
    2: OUT, 3: OUT, 6: OUT, 8: OUT, 13: OUT,
}
PUSULA_TYPE_NAMES = {
    1: 'Alış Faturası',
    2: 'Alış İade Faturası',
    3: 'Satış Faturası',
    4: 'Satış İade Faturası',
    5: 'Alınan Fiyat Farkı Faturası',
    6: 'Verilen Fiyat Farkı Faturası',
    7: 'Alınan Hizmet Faturası',
    8: 'Hasta Faturası',
    9: 'Provizyon Durumundaki Fatura',
    13: 'Fatura',
}
# Finans.Fatura.State (MS_Description): 0 cancelled, 1 first record, 2 merged.
PUSULA_INVALID_STATE = {0: 'cancelled', 2: 'merged'}
# Finans.Fatura.FaturaEvrakTipiId (MS_Description): 1 invoice, 2 receipt,
# 3 other. Value 4 is undocumented and stays valid until clarified.
PUSULA_INVALID_DOCUMENT_KIND = {2: 'receipt', 3: 'other_document'}

# Cerebrum SATINALMAFATURA.FISTIPI_ID -> direction (checked with BORCALACAK).
CEREBRUM_DIRECTION = {27: IN, 29: IN, 25: OUT, 28: OUT, 35: OUT}
CEREBRUM_TYPE_NAMES = {
    25: 'Satış Faturası',
    27: 'Alış Faturası',
    28: 'Alış İade Faturası',
    29: 'Satış İade Faturası',
    35: 'İskonto Faturası',
}
# IPTAL = 1 carries a full cancellation trail; IPTAL = 2 has none and never
# reached Logo, so it is treated as an incomplete record (inference).
CEREBRUM_INVALID_IPTAL = {1: 'cancelled', 2: 'incomplete'}

AMOUNT_TOLERANCE = 0.02
# Pusula computes header totals from rounded lines: in the October 2026 dry
# run 605 of 58,573 outgoing matches differed by less than 1 TL.
ROUNDING_TOLERANCE = 1.00

AMOUNT_EXACT = 'exact'
AMOUNT_ROUNDING = 'rounding'
AMOUNT_DIFFERENT = 'different'

# Verdict per source system.
FOUND = 'found'
MULTIPLE = 'multiple'
DATE_MISMATCH = 'date_mismatch'
INVALID = 'invalid'
MISSING = 'missing'

# Overall status across both systems.
BOTH = 'both'

_NON_DIGIT = re.compile(r'\D')


def normalize_number(value):
    """Invoice numbers compare case- and whitespace-insensitively."""
    return (value or '').strip().upper()


def normalize_identity(value):
    """VKN/TCKN digits only; empty when nothing usable is left."""
    return _NON_DIGIT.sub('', value or '')


@dataclasses.dataclass(frozen=True)
class SourceRecord:
    """Narrow, read-only view of one Pusula or Cerebrum invoice."""
    system: str
    record_id: int
    numbers: tuple
    date: datetime.date
    direction: str
    type_id: int
    unit_id: int
    amount_try: float = None
    identity: str = None
    ettn: str = None
    invalid_reason: str = None
    document_kind: int = None

    @property
    def is_valid(self):
        return not self.invalid_reason


@dataclasses.dataclass(frozen=True)
class GibInvoice:
    """The izibiz side of the comparison."""
    invoice_id: int
    company_id: int
    direction: str
    number: str
    date: datetime.date
    amount_try: float = None          # tax inclusive, TL
    counterparty_identity: str = None
    ettn: str = None
    payable_amount_try: float = None  # differs from amount_try under withholding


@dataclasses.dataclass(frozen=True)
class SourceVerdict:
    status: str
    record: SourceRecord = None
    candidates: tuple = ()            # in-scope candidates, for the multiple case
    candidate_count: int = 0
    found_in_other_company: bool = False
    amount_diff: float = None
    amount_basis: str = None          # 'inclusive' or 'payable'
    identity_match: bool = None
    ettn_match: bool = None


def build_index(records):
    """Map every normalised number of every record to its records."""
    index = {}
    for record in records:
        for number in {normalize_number(n) for n in record.numbers if n}:
            index.setdefault(number, []).append(record)
    return index


def _day_distance(record, date):
    if record.date is None or date is None:
        return 10 ** 6
    return abs((record.date - date).days)


def _decide(date, candidates):
    """Apply the verdict ladder to candidates already filtered by scope."""
    same_date = [c for c in candidates if c.date == date]
    valid_same_date = [c for c in same_date if c.is_valid]
    if len(valid_same_date) == 1:
        return FOUND, valid_same_date[0]
    if len(valid_same_date) > 1:
        return MULTIPLE, None
    valid_other_date = [c for c in candidates if c.is_valid]
    if valid_other_date:
        return DATE_MISMATCH, min(valid_other_date, key=lambda c: _day_distance(c, date))
    if candidates:
        return INVALID, (same_date or sorted(candidates, key=lambda c: _day_distance(c, date)))[0]
    return MISSING, None


def judge(invoice, index, scope_units):
    """Verdict of one source system for one izibiz invoice.

    `index` comes from build_index() for a single system and `scope_units`
    holds the Pusula branch or Cerebrum company ids mapped to the invoice's
    Odoo company.
    """
    candidates = [
        c for c in index.get(normalize_number(invoice.number), ())
        if c.direction == invoice.direction
    ]
    in_scope = [c for c in candidates if c.unit_id in scope_units]
    status, record = _decide(invoice.date, in_scope)

    found_elsewhere = False
    if status == MISSING:
        out_of_scope = [c for c in candidates if c.unit_id not in scope_units]
        found_elsewhere = _decide(invoice.date, out_of_scope)[0] == FOUND

    amount_diff = amount_basis = identity_match = ettn_match = None
    if record is not None:
        if record.amount_try is not None:
            # Cerebrum stores the payable amount on withholding invoices (44 of
            # 46 incoming differences in the October 2026 dry run), so the
            # closer of the two GİB totals is the comparison basis.
            options = [
                (round(record.amount_try - total, 2), basis)
                for total, basis in ((invoice.amount_try, 'inclusive'),
                                     (invoice.payable_amount_try, 'payable'))
                if total is not None
            ]
            if options:
                amount_diff, amount_basis = min(options, key=lambda o: abs(o[0]))
        if invoice.direction == IN:
            ours, theirs = normalize_identity(record.identity), normalize_identity(invoice.counterparty_identity)
            if ours and theirs:
                identity_match = ours == theirs
        if record.system == PUSULA and invoice.direction == OUT and record.ettn and invoice.ettn:
            ettn_match = record.ettn.strip().upper() == invoice.ettn.strip().upper()

    return SourceVerdict(
        status=status,
        record=record,
        candidates=tuple(in_scope),
        candidate_count=len(in_scope),
        found_in_other_company=found_elsewhere,
        amount_diff=amount_diff,
        amount_basis=amount_basis,
        identity_match=identity_match,
        ettn_match=ettn_match,
    )


def overall_status(pusula_verdict, cerebrum_verdict):
    """Combine both verdicts; a document found in both systems is a finding."""
    p, c = pusula_verdict.status, cerebrum_verdict.status
    if p == FOUND and c == FOUND:
        return BOTH
    if p == FOUND:
        return PUSULA
    if c == FOUND:
        return CEREBRUM
    for status in (MULTIPLE, DATE_MISMATCH, INVALID):
        if status in (p, c):
            return status
    return MISSING


def amount_level(verdict):
    """exact (<= 0.02 TL), rounding (<= 1 TL) or different; None without amounts."""
    if verdict.amount_diff is None:
        return None
    diff = abs(verdict.amount_diff)
    if diff <= AMOUNT_TOLERANCE:
        return AMOUNT_EXACT
    if diff <= ROUNDING_TOLERANCE:
        return AMOUNT_ROUNDING
    return AMOUNT_DIFFERENT


def amount_mismatch(verdict):
    return amount_level(verdict) == AMOUNT_DIFFERENT
