"""Read-only Cerebrum (Oracle 11.2, schema GUVEN) invoice reader.

The server is older than 12.1, so python-oracledb must run in thick mode
with Oracle Instant Client 19c. On Linux the client libraries have to be on
LD_LIBRARY_PATH before the process starts; init_oracle_client(lib_dir=...)
alone fails with DPI-1047.

SATINALMAFATURA is small (about 13k invoice rows a year), so a run reads every
in-scope invoice since the start date and matches in memory. That also
catches invoices entered months late. FATURANO has no index of its own.
"""
import datetime

from . import matching

_SELECT_SINCE = """
SELECT SF.ID, SF.FISTIPI_ID, SF.IPTAL, SF.SIRKET_ID, SF.FATURANO, SF.E_FATURA_BELGE_NO,
       SF.FATURATARIH, SF.TOPLAMFIYAT, FK.UQ_VERGINO
FROM GUVEN.SATINALMAFATURA SF
LEFT JOIN GUVEN.FIRMAKIMLIK FK ON FK.ID = SF.KUR_ID
WHERE SF.FISTIPI_ID IN ({types}) AND SF.FATURATARIH >= :date_from
"""

_thick_mode_ready = False


class CerebrumError(Exception):
    pass


def _ensure_thick_mode():
    global _thick_mode_ready
    if _thick_mode_ready:
        return
    try:
        import oracledb
    except ImportError as exc:
        raise CerebrumError("oracledb kütüphanesi yüklü değil") from exc
    oracledb.init_oracle_client()
    _thick_mode_ready = True


class CerebrumClient:
    """Context manager holding one connection for one comparison run."""

    def __init__(self, host, port, service_name, user, password, call_timeout_ms=120000):
        self._host = host
        self._port = int(port)
        self._service_name = service_name
        self._user = user
        self._password = password
        self._call_timeout_ms = call_timeout_ms
        self._conn = None

    def __enter__(self):
        _ensure_thick_mode()
        import oracledb
        dsn = oracledb.makedsn(self._host, self._port, service_name=self._service_name)
        self._conn = oracledb.connect(user=self._user, password=self._password, dsn=dsn)
        self._conn.call_timeout = self._call_timeout_ms
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
            cursor.execute("SELECT ID FROM GUVEN.SATINALMAFATURA WHERE ROWNUM = 1")
            cursor.fetchall()
        finally:
            cursor.close()

    def fetch_since(self, date_from):
        """Return every in-scope invoice dated on or after `date_from`."""
        types = ', '.join(str(t) for t in sorted(matching.CEREBRUM_DIRECTION))
        cursor = self._conn.cursor()
        try:
            cursor.execute(_SELECT_SINCE.format(types=types), date_from=date_from)
            return [self._to_record(row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    @staticmethod
    def _to_record(row):
        (record_id, type_id, iptal, company, number, document_number,
         date, amount, identity) = row
        direction = matching.CEREBRUM_DIRECTION[type_id]
        # Incoming: the GİB number sits in FATURANO. Outgoing: in
        # E_FATURA_BELGE_NO, FATURANO being the internal number.
        numbers = (number,) if direction == matching.IN else (document_number, number)
        if isinstance(date, datetime.datetime):
            date = date.date()
        reason = None if iptal == 0 else matching.CEREBRUM_INVALID_IPTAL.get(iptal, 'unknown_iptal')
        return matching.SourceRecord(
            system=matching.CEREBRUM,
            record_id=int(record_id),
            numbers=tuple(n for n in numbers if n),
            date=date,
            direction=direction,
            type_id=int(type_id),
            unit_id=int(company),
            amount_try=float(amount) if amount is not None else None,
            identity=identity,
            invalid_reason=reason,
        )
