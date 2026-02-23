import logging
import time
from datetime import date, timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# UNION of DATE_ and DOCDATE ranges (deduplicated by LOGICALREF)
_LOGO_SQL = """\
SELECT
    inv.LOGICALREF,
    inv.FICHENO,
    inv.DOCODE,
    inv.TRCODE,
    inv.DATE_,
    inv.DOCDATE,
    inv.CANCELLED,
    inv.NETTOTAL,
    inv.TRCURR,
    inv.TRRATE,
    inv.TRNET,
    cl.TAXNR,
    cl.TCKNO
FROM {invoice_table} inv
LEFT JOIN {clcard_table} cl ON inv.CLIENTREF = cl.LOGICALREF
WHERE CAST(inv.DOCDATE AS DATE) BETWEEN %s AND %s

UNION

SELECT
    inv.LOGICALREF,
    inv.FICHENO,
    inv.DOCODE,
    inv.TRCODE,
    inv.DATE_,
    inv.DOCDATE,
    inv.CANCELLED,
    inv.NETTOTAL,
    inv.TRCURR,
    inv.TRRATE,
    inv.TRNET,
    cl.TAXNR,
    cl.TCKNO
FROM {invoice_table} inv
LEFT JOIN {clcard_table} cl ON inv.CLIENTREF = cl.LOGICALREF
WHERE CAST(inv.DATE_ AS DATE) BETWEEN %s AND %s
"""


def _row_to_vals(row, company_id):
    """Convert a MSSQL row dict to guven.logo.fatura field values."""
    def _to_date(val):
        if val is None:
            return False
        if isinstance(val, date):
            # datetime is subclass of date; always return pure date
            return val if type(val) is date else val.date()
        return False

    trcode = str(row['TRCODE']) if row['TRCODE'] is not None else False
    cancelled = str(row['CANCELLED']) if row['CANCELLED'] is not None else False

    return {
        'company_id': company_id,
        'logo_id': row['LOGICALREF'],
        'fatura_no_1': row['FICHENO'] or False,
        'fatura_no_2': row['DOCODE'] or False,
        'fatura_tipi': trcode,
        'fatura_tarihi_1': _to_date(row['DATE_']),
        'fatura_tarihi_2': _to_date(row['DOCDATE']),
        'iptal_durumu': cancelled,
        'fatura_tutari': round(float(row['NETTOTAL'] or 0), 8),
        'para_birimi': str(row['TRCURR']) if row['TRCURR'] is not None else False,
        'kur': round(float(row['TRRATE'] or 0), 8),
        'doviz_tutari': round(float(row['TRNET'] or 0), 8),
        'vkn': (row['TAXNR'] or '').strip() or False,
        'tckn': (row['TCKNO'] or '').strip() or False,
    }


class GuvenLogoFatura(models.Model):
    _name = 'guven.logo.fatura'
    _description = 'Logo Fatura'
    _order = 'fatura_tarihi_1 desc, id desc'
    _check_company_auto = True

    # Advisory lock ID for Logo sync cron
    _LOCK_LOGO_SYNC = 737004

    company_id = fields.Many2one(
        'res.company', string='Şirket', required=True, index=True,
        default=lambda self: self.env.company,
    )
    logo_id = fields.Integer(string='Logo ID', required=True, index=True)
    fatura_no_1 = fields.Char(string='Fatura No 1', index=True)
    fatura_no_2 = fields.Char(string='Fatura No 2', index=True)
    fatura_tipi = fields.Selection(
        selection=[
            ('1', 'Alış Faturası'),
            ('3', 'Alış İade Faturası'),
            ('4', 'Alış İrsaliyeli Fatura'),
            ('6', 'Satış Faturası'),
            ('7', 'Satış İade Faturası'),
            ('8', 'Satış İrsaliyeli Fatura'),
            ('9', 'Satış İade İrsaliyeli'),
            ('12', 'Nadir'),
            ('13', 'Gelen E-Fatura'),
        ],
        string='Fatura Tipi',
    )
    fatura_tarihi_1 = fields.Date(string='Fatura Tarihi 1')
    fatura_tarihi_2 = fields.Date(string='Fatura Tarihi 2')
    iptal_durumu = fields.Selection(
        selection=[('0', 'Aktif'), ('1', 'İptal')],
        string='İptal Durumu',
    )
    fatura_tutari = fields.Float(string='Fatura Tutarı', digits=(16, 8))
    para_birimi = fields.Char(string='Para Birimi', size=3)
    kur = fields.Float(string='Kur', digits=(16, 8))
    doviz_tutari = fields.Float(string='Döviz Tutarı', digits=(16, 8))
    vkn = fields.Char(string='VKN', size=11)
    tckn = fields.Char(string='TCKN', size=11)

    _unique_logo = models.Constraint(
        'UNIQUE (logo_id, company_id)',
        'Bu Logo ID ile kayıt zaten mevcut!',
    )

    # ── Advisory Lock helpers ────────────────────────────────────

    @api.model
    def _try_advisory_lock(self, lock_id):
        """PostgreSQL session-level advisory lock almayı dene."""
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
        return self.env.cr.fetchone()[0]

    @api.model
    def _release_advisory_lock(self, lock_id):
        """PostgreSQL session-level advisory lock'u serbest bırak."""
        self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))

    # ── Per-Company Sync ─────────────────────────────────────────

    @api.model
    def _sync_company(self, company, date_from, date_to):
        """Fetch invoices from Logo MSSQL and upsert into guven.logo.fatura."""
        import pymssql

        creds = company.get_logo_credentials()
        conn = pymssql.connect(
            server=creds['server'],
            port=str(creds['port']),
            user=creds['username'],
            password=creds['password'],
            database=creds['database'],
            timeout=30,
            login_timeout=30,
            charset='UTF-8',
        )
        try:
            cursor = conn.cursor(as_dict=True)
            sql = _LOGO_SQL.format(
                invoice_table=creds['invoice_table'],
                clcard_table=creds['clcard_table'],
            )
            cursor.execute(sql, (date_from, date_to, date_from, date_to))
            rows = cursor.fetchall()
        finally:
            conn.close()

        fetched = len(rows)

        # ── Upsert: Logo'dan dönen kayıtları oluştur/güncelle ───
        to_create = []
        to_update = []
        if rows:
            logo_ids = [r['LOGICALREF'] for r in rows]
            existing = self.with_company(company).search([
                ('company_id', '=', company.id),
                ('logo_id', 'in', logo_ids),
            ])
            existing_map = {rec.logo_id: rec for rec in existing}

            for row in rows:
                vals = _row_to_vals(row, company.id)
                rec = existing_map.get(row['LOGICALREF'])
                if rec:
                    changed = {
                        k: v for k, v in vals.items()
                        if k != 'company_id' and rec[k] != v
                    }
                    if changed:
                        to_update.append((rec, changed))
                else:
                    to_create.append(vals)

            if to_create:
                self.with_company(company).create(to_create)
            for rec, changed in to_update:
                rec.write(changed)

        # ── Orphan tespiti: Logo'da silinen kayıtları Odoo'dan temizle ──
        all_odoo_in_range = self.with_company(company).search([
            ('company_id', '=', company.id),
            '|',
            '&', ('fatura_tarihi_2', '>=', date_from), ('fatura_tarihi_2', '<=', date_to),
            '&', ('fatura_tarihi_1', '>=', date_from), ('fatura_tarihi_1', '<=', date_to),
        ])
        returned_logo_ids = {r['LOGICALREF'] for r in rows}
        orphans = all_odoo_in_range.filtered(
            lambda r: r.logo_id not in returned_logo_ids
        )
        deleted_count = len(orphans)
        if orphans:
            orphans.unlink()

        return {
            'fetched': fetched,
            'created': len(to_create),
            'updated': len(to_update),
            'deleted': deleted_count,
        }

    # ── Cron Entry Point ─────────────────────────────────────────

    @api.model
    def _cron_sync_logo(self):
        """Tüm şirketler için Logo MSSQL fatura sync (30 günlük bloklar)."""
        try:
            import pymssql  # noqa: F401
        except ImportError:
            _logger.warning(
                "[GUVEN-LOGO] pymssql kütüphanesi yüklü değil, cron atlanıyor."
            )
            return

        if not self._try_advisory_lock(self._LOCK_LOGO_SYNC):
            _logger.info("[GUVEN-LOGO] Logo sync zaten çalışıyor, atlanıyor.")
            return

        try:
            t0 = time.time()
            today = fields.Date.today()
            companies = self.env['res.company'].sudo().search([
                ('logo_auto_sync', '=', True),
            ])

            total_created = total_updated = total_deleted = 0

            for company in companies:
                if not company.has_logo_credentials():
                    continue

                lookback = company.logo_sync_lookback_days or 30
                min_start = today - timedelta(days=lookback)

                # Bugünün turu zaten tamamlandıysa bu şirketi atla
                last_completed = company.logo_sync_last_completed_date
                if last_completed and last_completed >= today:
                    continue

                # Cursor'ı belirle
                cursor_date = company.logo_sync_cursor_date
                if not cursor_date:
                    cursor_date = min_start
                elif last_completed and last_completed < today:
                    cursor_date = min_start

                # Güvenlik: lookback_days küçültüldüyse cursor'ı düzelt
                if cursor_date < min_start:
                    cursor_date = min_start

                # Cursor zaten bugüne ulaştıysa turu tamamla
                if cursor_date >= today:
                    company.sudo().write({
                        'logo_sync_last_completed_date': today,
                    })
                    self.env.cr.commit()
                    continue

                block_end = min(cursor_date + timedelta(days=29), today)

                _logger.info(
                    "[GUVEN-LOGO] %s: %s → %s",
                    company.name, cursor_date, block_end,
                )

                try:
                    result = self._sync_company(company, cursor_date, block_end)
                except Exception:
                    _logger.exception(
                        "[GUVEN-LOGO] %s: MSSQL sync hatası", company.name,
                    )
                    continue

                total_created += result['created']
                total_updated += result['updated']
                total_deleted += result['deleted']

                # Logo eşleştirme
                try:
                    self.env['guven.fatura']._match_logo_invoices(
                        cursor_date, block_end, [company.id],
                    )
                except Exception:
                    _logger.exception(
                        "[GUVEN-LOGO] %s: Logo eşleştirme hatası", company.name,
                    )

                # Cursor'ı ilerlet
                next_cursor = block_end + timedelta(days=1)
                write_vals = {'logo_sync_cursor_date': next_cursor}
                if next_cursor >= today:
                    write_vals['logo_sync_last_completed_date'] = today
                company.sudo().write(write_vals)
                self.env.cr.commit()

                _logger.info(
                    "[GUVEN-LOGO] %s: %d yeni, %d güncellenen, %d silinen",
                    company.name, result['created'], result['updated'],
                    result['deleted'],
                )

            elapsed = time.time() - t0
            _logger.info(
                "[GUVEN-LOGO] Cron tamamlandı. %d yeni, %d günc., %d silinen. "
                "Süre: %.1f sn",
                total_created, total_updated, total_deleted, elapsed,
            )
        except Exception:
            _logger.exception("[GUVEN-LOGO] Cron hatası")
        finally:
            self._release_advisory_lock(self._LOCK_LOGO_SYNC)
