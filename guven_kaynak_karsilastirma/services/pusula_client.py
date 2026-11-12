"""Read-only Pusula (SQL Server, PusulaHBYS) invoice reader.

Only the columns the comparison needs are selected. Patient-linked rows
(HastaId set) never return a counterparty identity, and nothing is read from
the Hasta schema. Pusula is the live hospital database without a readable
replica, so reads go through the MatbuFaturaNo index in bounded batches.
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
       CAST(FT.UUID AS nvarchar(50))
FROM Finans.Fatura FT WITH (NOLOCK)
LEFT JOIN Finans.Cari CR WITH (NOLOCK) ON CR.Id = FT.CariId
WHERE FT.MatbuFaturaNo IN ({placeholders})
"""

PROVISION_TYPE = 9


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
                for row in cursor.fetchall():
                    record = self._to_record(row)
                    if record is not None:
                        records.append(record)
        finally:
            cursor.close()
        return records

    def _to_record(self, row):
        (record_id, type_id, document_kind, state, unit_id, number,
         date, amount, identity, ettn) = row
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
        return matching.SourceRecord(
            system=matching.PUSULA,
            record_id=record_id,
            numbers=(number,),
            date=date,
            direction=direction,
            type_id=type_id,
            unit_id=unit_id,
            amount_try=float(amount) if amount is not None else None,
            identity=identity,
            ettn=ettn,
            invalid_reason=reason,
            document_kind=document_kind,
        )
