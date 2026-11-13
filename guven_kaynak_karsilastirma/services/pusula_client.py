"""Read-only Pusula (SQL Server, PusulaHBYS) invoice reader.

Only the columns the comparison needs are selected. Patient-linked rows
(HastaId set) never return a counterparty identity, and nothing is read from
the Hasta schema. Pusula is the live hospital database without a readable
replica, so reads go through indexes in bounded batches.

TL amount of an invoice (all rules measured on prod, 2026-11-13):

1. Type 13 institution invoices: no single total is right everywhere, so
   the record offers several and matching keeps the one closest to GİB.
   Primary: the TL line sum (FaturaKalem.KdvliTutar) of the valid
   (State = 1) type 9 children; it fixed 54 e-faturas whose header held a
   single provision (14,000 of 472,000). Alternatives: the header (right on
   14 e-faturas whose children lines cover only part of the invoice) and
   the line sum of all children (3 invoices with cancelled children).
2. Other foreign-currency invoices (ParaBirimiId <> 1) with lines of their
   own: the TL line sum. Type 8 headers are in the document currency
   (8,936 USD/EUR patient invoices looked ~98 % short before this).
3. Otherwise the header ToplamKdvliTutar, which is TL.

Withholding: when the header carries TevkifatOranId (Ortak.TevkifatOrani
Pay/Payda), the payable amount is ToplamKdvsizTutar + ToplamKdvTutari *
(1 - Pay/Payda). GİB shows that payable amount on such invoices, e.g. type 4
sales returns: 99.21 + 9.92 * 0.5 = 104.17 (1,007 rows were ~4.8 % off).
"""
import collections

from . import matching

# Every type is read (9 included) so that unexpected values can be counted;
# type 9 is dropped in Python because type 13 carries the same document.
_SELECT_BY_NUMBERS = """
SELECT FT.Id, FT.FaturaTipiId, FT.FaturaEvrakTipiId, FT.State, FT.SubeId,
       CAST(FT.MatbuFaturaNo AS nvarchar(200)), FT.Tarih, FT.ToplamKdvliTutar,
       CASE WHEN FT.HastaId IS NULL
            THEN COALESCE(NULLIF(LTRIM(RTRIM(FT.VergiNo)), N''), CR.VergiNo,
                          CAST(CR.TcKimlikNo AS nvarchar(20)))
       END,
       CAST(FT.UUID AS nvarchar(50)), FT.ParaBirimiId,
       FT.ToplamKdvsizTutar, FT.ToplamKdvTutari, TVK.Pay, TVK.Payda
FROM Finans.Fatura FT WITH (NOLOCK)
LEFT JOIN Finans.Cari CR WITH (NOLOCK) ON CR.Id = FT.CariId
LEFT JOIN Ortak.TevkifatOrani TVK WITH (NOLOCK) ON TVK.Id = FT.TevkifatOranId
WHERE FT.MatbuFaturaNo IN ({placeholders})
"""

# Own line totals of foreign-currency invoices (IX_FaturaKalem_FaturaId).
_SELECT_LINE_TOTALS = """
SELECT K.FaturaId, SUM(K.KdvliTutar), SUM(K.KdvliTutarDoviz), COUNT(*)
FROM Finans.FaturaKalem K WITH (NOLOCK)
WHERE K.FaturaId IN ({placeholders}) AND K.State <> 0
GROUP BY K.FaturaId
"""

# Children line totals of type 13 invoices (IX_Fatura_ParentFatura):
# valid children (TL, currency, line count) and all children (TL).
_SELECT_CHILD_TOTALS = """
SELECT C.ParentFaturaId,
       SUM(CASE WHEN C.State = 1 THEN K.KdvliTutar END),
       SUM(CASE WHEN C.State = 1 THEN K.KdvliTutarDoviz END),
       SUM(CASE WHEN C.State = 1 THEN 1 ELSE 0 END),
       SUM(K.KdvliTutar)
FROM Finans.Fatura C WITH (NOLOCK)
JOIN Finans.FaturaKalem K WITH (NOLOCK) ON K.FaturaId = C.Id AND K.State <> 0
WHERE C.ParentFaturaId IN ({placeholders})
GROUP BY C.ParentFaturaId
"""

PROVISION_TYPE = 9
INSTITUTION_TYPE = 13

# Pusula.ParaBirimi. Row 24 is labelled "TRY" but named "Avustralya
# Doları"; only 1 is treated as TL.
TL_CURRENCY_ID = 1
CURRENCY_CODES = {
    2: 'USD', 3: 'EUR', 4: 'CAD', 5: 'GBP', 6: 'JPY', 7: 'SAR', 10: 'AUD', 11: 'DKK',
    12: 'CHF', 13: 'SEK', 14: 'KWD', 15: 'NOK', 21: 'ILS', 25: 'RON', 26: 'BGN',
    27: 'RUB', 28: 'AZN', 29: 'CNY', 30: 'PKR', 31: 'GEL',
}


def _money(value):
    return round(float(value), 2) if value is not None else None


class PusulaError(Exception):
    pass


class PusulaClient:
    """Context manager holding one connection for one comparison run."""

    BATCH_SIZE = 1000

    def __init__(self, server, port, database, user, password,
                 login_timeout=30, query_timeout=120):
        self._params = {
            'server': server,
            'port': str(port),
            'database': database,
            'user': user,
            'password': password,
            'login_timeout': login_timeout,
            'timeout': query_timeout,
            'appname': 'guven-kaynak-karsilastirma',
        }
        self._conn = None
        self.unknown_types = collections.Counter()

    def __enter__(self):
        try:
            import pymssql
        except ImportError as exc:
            raise PusulaError("pymssql kütüphanesi yüklü değil") from exc
        self._conn = pymssql.connect(**self._params)
        cursor = self._conn.cursor()
        cursor.execute("SET LOCK_TIMEOUT 5000")
        cursor.close()
        return self

    def __exit__(self, *exc_info):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        return False

    def ping(self):
        """Cheapest possible check that the account can read invoices."""
        cursor = self._conn.cursor()
        try:
            cursor.execute("SELECT TOP (1) Id FROM Finans.Fatura WITH (NOLOCK)")
            cursor.fetchall()
        finally:
            cursor.close()

    def _totals(self, cursor, sql, ids):
        if not ids:
            return {}
        cursor.execute(sql.format(placeholders=', '.join(['%s'] * len(ids))), tuple(ids))
        return {row[0]: row[1:] for row in cursor.fetchall()}

    def fetch_by_numbers(self, numbers):
        """Return SourceRecords whose MatbuFaturaNo is in `numbers`."""
        wanted = sorted({matching.normalize_number(n) for n in numbers if n})
        records = []
        cursor = self._conn.cursor()
        try:
            for start in range(0, len(wanted), self.BATCH_SIZE):
                batch = wanted[start:start + self.BATCH_SIZE]
                sql = _SELECT_BY_NUMBERS.format(placeholders=', '.join(['%s'] * len(batch)))
                cursor.execute(sql, tuple(batch))
                rows = cursor.fetchall()
                institution = sorted({row[0] for row in rows if row[1] == INSTITUTION_TYPE})
                foreign = sorted({row[0] for row in rows
                                  if row[10] not in (None, TL_CURRENCY_ID)
                                  and row[1] not in (PROVISION_TYPE, INSTITUTION_TYPE)})
                children = self._totals(cursor, _SELECT_CHILD_TOTALS, institution)
                lines = self._totals(cursor, _SELECT_LINE_TOTALS, foreign)
                for row in rows:
                    record = self._to_record(row, lines.get(row[0]), children.get(row[0]))
                    if record is not None:
                        records.append(record)
        finally:
            cursor.close()
        return records

    def _to_record(self, row, lines=None, children=None):
        """`lines`: (TL sum, currency sum, line count) of the invoice's own lines.

        `children`: (valid TL sum, valid currency sum, valid line count,
        all-children TL sum) of a type 13 invoice. Either may be None.
        """
        (record_id, type_id, document_kind, state, unit_id, number, date, amount,
         identity, ettn, currency_id, net, vat, withholding_num, withholding_den) = row
        if type_id == PROVISION_TYPE:
            return None
        direction = matching.PUSULA_DIRECTION.get(type_id)
        if direction is None:
            self.unknown_types[type_id] += 1
            return None
        if state == 1:
            reason = matching.PUSULA_INVALID_DOCUMENT_KIND.get(document_kind)
        else:
            reason = matching.PUSULA_INVALID_STATE.get(state, 'unknown_state')

        currency = None
        if currency_id not in (None, TL_CURRENCY_ID):
            currency = CURRENCY_CODES.get(currency_id, 'PB%s' % currency_id)
        amount_try = _money(amount)
        amount_currency = amount_payable = None
        alternatives = ()
        if type_id == INSTITUTION_TYPE:
            header = amount_try
            if children and children[2]:
                amount_try = _money(children[0])
                amount_currency = _money(children[1]) if currency else None
            candidates = (header, _money(children[3]) if children else None)
            alternatives = tuple(dict.fromkeys(a for a in candidates if a is not None and a != amount_try))
        elif currency and lines and lines[2]:
            amount_try = _money(lines[0])
            amount_currency = _money(lines[1])
        elif not currency and withholding_num and withholding_den and net is not None and vat is not None:
            # Header amounts are TL only on TL invoices; withholding is
            # derived from them alone.
            amount_payable = _money(float(net) + float(vat) * (1 - float(withholding_num) / float(withholding_den)))

        return matching.SourceRecord(
            system=matching.PUSULA,
            record_id=record_id,
            numbers=(number,),
            date=date,
            direction=direction,
            type_id=type_id,
            unit_id=unit_id,
            amount_try=amount_try,
            identity=identity,
            ettn=ettn,
            invalid_reason=reason,
            document_kind=document_kind,
            currency=currency,
            amount_currency=amount_currency,
            amount_payable_try=amount_payable,
            amount_alternatives=alternatives,
        )
