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

    # TRCODE → direction mapping (Logo fatura tipi → GİB yönü)
    _TRCODE_DIRECTION = {
        '1': 'IN', '3': 'IN', '4': 'IN', '13': 'IN',      # Alış = GİB Gelen
        '6': 'OUT', '7': 'OUT', '8': 'OUT', '9': 'OUT',    # Satış = GİB Giden
    }

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

    # --- GİB Eşleşmesi (Logo → GİB ters eşleştirme) ---
    gib_fatura_ids = fields.Many2many(
        'guven.fatura',
        'guven_logo_fatura_gib_fatura_rel',
        'logo_fatura_id', 'fatura_id',
        string='GİB Faturaları', copy=False,
    )
    gib_fatura_count = fields.Integer(string='GİB Eşleşen Kayıt', default=0, copy=False)
    gib_fatura_no = fields.Char(string='GİB Fatura No', copy=False)
    gib_fatura_tarihi = fields.Date(string='GİB Fatura Tarihi', copy=False)
    gib_fatura_tutari = fields.Float(string='GİB Fatura Tutarı (TRY)', digits=(16, 8), copy=False)
    gib_kimlik = fields.Char(string='GİB VKN/TCKN', size=11, copy=False)
    gib_kaynak = fields.Selection(
        [('e-fatura-izibiz', 'E-Fatura (izibiz)'),
         ('e-arsiv-izibiz', 'E-Arşiv (izibiz)'),
         ('e-arsiv-gibexcel', 'E-Arşiv (GİB Excel)'),
         ('e-fatura-qnbexcel', 'E-Fatura (QNB Excel)'),
         ('e-arsiv-qnbexcel', 'E-Arşiv (QNB Excel)')],
        string='GİB Kaynak', copy=False,
    )
    tutar_farki_var = fields.Boolean(string='Tutar Farkı Var', default=False, copy=False)
    tutar_farki = fields.Float(string='Tutar Farkı', digits=(16, 8), copy=False)
    kimlik_farkli = fields.Boolean(string='Kimlik Farklı', default=False, copy=False)
    fatura_tarihi_farkli = fields.Boolean(string='Tarih Farklı', default=False, copy=False)
    gib_karsilastirma_html = fields.Html(
        string='Karşılaştırma', compute='_compute_gib_karsilastirma_html', sanitize=False,
    )
    gib_notes = fields.Text(string='GİB Eşleşme Notları', copy=False)

    _unique_logo = models.Constraint(
        'UNIQUE (logo_id, company_id)',
        'Bu Logo ID ile kayıt zaten mevcut!',
    )

    # ── Computed: GİB Karşılaştırma HTML ─────────────────────────

    @api.depends(
        'gib_fatura_count', 'gib_kimlik',
        'tutar_farki_var', 'tutar_farki', 'kimlik_farkli',
        'fatura_tarihi_farkli',
    )
    def _compute_gib_karsilastirma_html(self):
        for rec in self:
            if not rec.gib_fatura_count:
                rec.gib_karsilastirma_html = False
                continue
            rec.gib_karsilastirma_html = rec._build_gib_karsilastirma_html()

    def _build_gib_karsilastirma_html(self):
        """Build compact comparison badge HTML for GİB matching."""
        checks = []

        # Kimlik
        if self.kimlik_farkli:
            checks.append(('Kimlik', 'Farklı', '#ef4444', '#fef2f2'))
        else:
            checks.append(('Kimlik', self.gib_kimlik or '—', '#10b981', '#f0fdf4'))

        # Tutar farkı
        if self.tutar_farki_var:
            farki = f"{self.tutar_farki:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            checks.append(('Tutar Farkı', farki, '#f59e0b', '#fffbeb'))
        else:
            checks.append(('Tutar', 'Eşit', '#10b981', '#f0fdf4'))

        # Tarih
        if self.fatura_tarihi_farkli:
            checks.append(('Tarih', 'Farklı', '#ef4444', '#fef2f2'))
        else:
            checks.append(('Tarih', 'Eşit', '#10b981', '#f0fdf4'))

        badges = ''
        for label, value, color, bg in checks:
            icon = (
                '<span style="margin-right:4px">&#10005;</span>'
                if color in ('#ef4444', '#f59e0b')
                else '<span style="margin-right:4px">&#10003;</span>'
            )
            badges += (
                f'<span style="display:inline-flex;align-items:center;'
                f'padding:4px 10px;margin:3px 4px;border-radius:6px;'
                f'font-size:12px;font-weight:600;'
                f'background:{bg};color:{color};'
                f'border:1px solid {color}20">'
                f'{icon}'
                f'<span style="color:#64748b;font-weight:400;margin-right:4px">'
                f'{label}:</span> {value}</span>'
            )

        return (
            f'<div style="display:flex;flex-wrap:wrap;align-items:center;'
            f'gap:2px;padding:4px 0">{badges}</div>'
        )

    # ── GİB Eşleştirme (Logo → GİB ters yön) ────────────────────

    def _process_single_gib_match(self, logo_rec, gib_rec, stats):
        """Tek eşleşme için fark analizi yap ve guven.logo.fatura'ya yaz."""
        # Tutar farkı
        logo_tutar = logo_rec.fatura_tutari or 0.0
        gib_tutar = gib_rec.payable_amount_try or 0.0
        farki = logo_tutar - gib_tutar
        tutar_farki_var = abs(farki) > 0.005

        # Direction: Logo TRCODE'dan belirle, fallback GİB direction
        direction = self._TRCODE_DIRECTION.get(logo_rec.fatura_tipi, gib_rec.direction)

        # Kimlik fark analizi
        if direction == 'IN':
            gib_kimlik = (gib_rec.sender or '').strip()
        elif direction == 'OUT':
            gib_kimlik = (gib_rec.receiver or '').strip()
        else:
            gib_kimlik = ''

        logo_vkn = (logo_rec.vkn or '').strip()
        logo_tckn = (logo_rec.tckn or '').strip()

        kimlik_eslesti = (
            (logo_vkn and gib_kimlik == logo_vkn)
            or (logo_tckn and gib_kimlik == logo_tckn)
        )
        kimlik_farkli = bool(gib_kimlik and (logo_vkn or logo_tckn) and not kimlik_eslesti)

        # Tarih farkı
        logo_tarihi = logo_rec.fatura_tarihi_1 or logo_rec.fatura_tarihi_2
        fatura_tarihi_farkli = bool(
            gib_rec.issue_date and logo_tarihi
            and gib_rec.issue_date != logo_tarihi
        )

        if tutar_farki_var:
            stats['tutar_farki'] += 1
        if kimlik_farkli:
            stats['kimlik_farkli'] += 1
        if fatura_tarihi_farkli:
            stats['fatura_tarihi_farkli'] += 1

        logo_rec.write({
            'gib_fatura_ids': [(6, 0, [gib_rec.id])],
            'gib_fatura_count': 1,
            'gib_fatura_no': gib_rec.invoice_id,
            'gib_fatura_tarihi': gib_rec.issue_date,
            'gib_fatura_tutari': gib_tutar,
            'gib_kimlik': gib_kimlik or False,
            'gib_kaynak': gib_rec.kaynak,
            'tutar_farki_var': tutar_farki_var,
            'tutar_farki': round(farki, 2),
            'kimlik_farkli': kimlik_farkli,
            'fatura_tarihi_farkli': fatura_tarihi_farkli,
            'gib_notes': False,
        })

    @api.model
    def _match_gib_invoices(self, date_from, date_to, company_ids):
        """Logo faturaları ile GİB kayıtlarını eşleştir (ters yön).

        Args:
            date_from: Başlangıç tarihi (fields.Date)
            date_to: Bitiş tarihi (fields.Date)
            company_ids: list of int — eşleştirilecek şirket ID'leri

        Returns:
            dict: Stats with total, matched_single, matched_multi, unmatched, etc.
        """
        GibFatura = self.env['guven.fatura']
        stats = {
            'total': 0,
            'matched_single': 0,
            'matched_multi': 0,
            'unmatched': 0,
            'tutar_farki': 0,
            'kimlik_farkli': 0,
            'fatura_tarihi_farkli': 0,
        }

        # 1. Logo kayıtlarını tarih aralığına göre al
        logo_recs = self.search([
            ('company_id', 'in', company_ids),
            '|',
            '&', ('fatura_tarihi_1', '>=', date_from), ('fatura_tarihi_1', '<=', date_to),
            '&', ('fatura_tarihi_2', '>=', date_from), ('fatura_tarihi_2', '<=', date_to),
        ])
        if not logo_recs:
            return stats
        stats['total'] = len(logo_recs)

        # 2. GİB faturalarını yükle (gvn_active=True, ilgili şirketler)
        gib_recs = GibFatura.search([
            ('gvn_active', '=', True),
            ('company_id', 'in', company_ids),
        ])

        # 3. Lookup dict: (company_id, invoice_id) → [guven.fatura, ...]
        gib_by_invoice_id = {}
        for gr in gib_recs:
            if gr.invoice_id:
                gib_by_invoice_id.setdefault(
                    (gr.company_id.id, gr.invoice_id), []
                ).append(gr)

        # 4. Her Logo kaydı için eşleşme ara
        for logo_rec in logo_recs:
            cid = logo_rec.company_id.id

            # fatura_no_1 ve fatura_no_2 ile arama, deduplicate
            matches_map = {}
            for fno in (logo_rec.fatura_no_1, logo_rec.fatura_no_2):
                if fno:
                    for gr in gib_by_invoice_id.get((cid, fno), []):
                        matches_map[gr.id] = gr

            matches = list(matches_map.values())
            match_count = len(matches)

            if match_count == 0:
                # Eşleşme yok — alanları temizle
                stats['unmatched'] += 1
                logo_rec.write({
                    'gib_fatura_ids': [(5, 0, 0)],
                    'gib_fatura_count': 0,
                    'gib_fatura_no': False,
                    'gib_fatura_tarihi': False,
                    'gib_fatura_tutari': 0.0,
                    'gib_kimlik': False,
                    'gib_kaynak': False,
                    'tutar_farki_var': False,
                    'tutar_farki': 0.0,
                    'kimlik_farkli': False,
                    'fatura_tarihi_farkli': False,
                    'gib_notes': False,
                })

            elif match_count == 1:
                stats['matched_single'] += 1
                self._process_single_gib_match(logo_rec, matches[0], stats)

            else:
                # Birden fazla eşleşme — kimlik ile daraltmayı dene
                direction = self._TRCODE_DIRECTION.get(logo_rec.fatura_tipi)
                logo_vkn = (logo_rec.vkn or '').strip()
                logo_tckn = (logo_rec.tckn or '').strip()

                if logo_vkn or logo_tckn:
                    vkn_filtered = []
                    for gr in matches:
                        if direction == 'IN':
                            gib_kimlik = (gr.sender or '').strip()
                        elif direction == 'OUT':
                            gib_kimlik = (gr.receiver or '').strip()
                        else:
                            gib_kimlik = (gr.sender or gr.receiver or '').strip()

                        if (logo_vkn and gib_kimlik == logo_vkn) or \
                           (logo_tckn and gib_kimlik == logo_tckn):
                            vkn_filtered.append(gr)
                else:
                    vkn_filtered = []

                if len(vkn_filtered) == 1:
                    stats['matched_single'] += 1
                    self._process_single_gib_match(logo_rec, vkn_filtered[0], stats)
                else:
                    # Daraltma başarısız — çoklu eşleşme
                    stats['matched_multi'] += 1
                    final_matches = vkn_filtered if len(vkn_filtered) > 1 else matches
                    final_count = len(final_matches)
                    lines = [f"GİB'de birden fazla kayıt bulundu ({final_count} adet):"]
                    for gr in final_matches:
                        tarih_str = gr.issue_date.strftime('%Y-%m-%d') if gr.issue_date else '-'
                        lines.append(
                            f"  - {gr.invoice_id}, "
                            f"Kaynak: {gr.kaynak or '-'}, "
                            f"Tutar: {gr.payable_amount_try:.2f}, "
                            f"Tarih: {tarih_str}"
                        )

                    logo_rec.write({
                        'gib_fatura_ids': [(6, 0, [gr.id for gr in final_matches])],
                        'gib_fatura_count': final_count,
                        'gib_fatura_no': False,
                        'gib_fatura_tarihi': False,
                        'gib_fatura_tutari': 0.0,
                        'gib_kimlik': False,
                        'gib_kaynak': False,
                        'tutar_farki_var': False,
                        'tutar_farki': 0.0,
                        'kimlik_farkli': False,
                        'fatura_tarihi_farkli': False,
                        'gib_notes': '\n'.join(lines),
                    })

        return stats

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
            charset='cp1254',
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

                # Logo eşleştirme (GİB → Logo)
                try:
                    self.env['guven.fatura']._match_logo_invoices(
                        cursor_date, block_end, [company.id],
                    )
                except Exception:
                    _logger.exception(
                        "[GUVEN-LOGO] %s: Logo eşleştirme hatası", company.name,
                    )

                # Ters eşleştirme (Logo → GİB)
                try:
                    self._match_gib_invoices(cursor_date, block_end, [company.id])
                except Exception:
                    _logger.exception(
                        "[GUVEN-LOGO] %s: Ters eşleştirme hatası", company.name,
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
