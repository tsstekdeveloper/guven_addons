"""izibiz -> Pusula / Cerebrum comparison: one row per valid izibiz invoice.

Design: docs-reports/kaynak_karsilastirma_tasarim.md. The row is evidence:
GİB and source columns are snapshots taken when the verdict was computed,
not related fields. Each part (GİB, Pusula, Cerebrum) has its own signature,
so a tour writes only the parts that changed and a failing source never
overwrites what the other source or a previous tour established.

guven.fatura is never written: its write() invalidates perfect_fit on the
Logo side and refuses locked records.
"""
import collections
import datetime
import hashlib
import json
import logging
import time

import psycopg2
from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import matching

_logger = logging.getLogger(__name__)

IZIBIZ_SOURCES = ['e-fatura-izibiz', 'e-arsiv-izibiz']
START_DATE_PARAM = 'guven_kaynak_karsilastirma.start_date'
FULL_SCAN_DATE_PARAM = 'guven_kaynak_karsilastirma.full_scan_date'
FULL_SCAN_CURSOR_PARAM = 'guven_kaynak_karsilastirma.full_scan_cursor'
DEFAULT_START_DATE = '2026-01-01'

UNCHECKED = 'unchecked'

SOURCE_STATUS = [
    (matching.FOUND, 'Bulundu'),
    (matching.MULTIPLE, 'Çoklu'),
    (matching.DATE_MISMATCH, 'Tarih Farklı'),
    (matching.INVALID, 'Geçersiz Kayıtta'),
    (matching.MISSING, 'Yok'),
    (UNCHECKED, 'Kontrol Edilemedi'),
]
OVERALL_STATUS = [
    (matching.PUSULA, 'Pusula'),
    (matching.CEREBRUM, 'Cerebrum'),
    (matching.BOTH, 'İkisinde'),
    (matching.MULTIPLE, 'Çoklu'),
    (matching.DATE_MISMATCH, 'Tarih Farklı'),
    (matching.INVALID, 'Geçersiz Kayıtta'),
    (matching.MISSING, 'Yok'),
    (UNCHECKED, 'Kontrol Edilemedi'),
]
AMOUNT_LEVEL = [
    (matching.AMOUNT_EXACT, 'Tam'),
    (matching.AMOUNT_ROUNDING, 'Yuvarlama'),
    (matching.AMOUNT_DIFFERENT, 'Farklı'),
]
AMOUNT_BASIS = [('inclusive', 'KDV Dahil'), ('payable', 'Ödenecek')]
MATCH = [('match', 'Uyumlu'), ('mismatch', 'Farklı')]
INVALID_REASON = [
    ('cancelled', 'İptal'),
    ('merged', 'Birleştirilmiş'),
    ('receipt', 'Fiş'),
    ('other_document', 'Diğer Evrak'),
    ('incomplete', 'Tamamlanmamış'),
    ('unknown_state', 'Bilinmeyen Durum'),
    ('unknown_iptal', 'Bilinmeyen İptal Değeri'),
]
PUSULA_TYPES = [(str(k), v) for k, v in sorted(matching.PUSULA_TYPE_NAMES.items())]
CEREBRUM_TYPES = [(str(k), v) for k, v in sorted(matching.CEREBRUM_TYPE_NAMES.items())]

_MATCH_VALUE = {True: 'match', False: 'mismatch'}
_RETRYABLE = (psycopg2.errors.SerializationFailure, psycopg2.errors.DeadlockDetected)


class _SourceFailure(Exception):
    pass


def _signature(values):
    payload = json.dumps(sorted(values.items()), default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _try_amount(currency, rate, amount, amount_try):
    if amount is None:
        return None
    if (currency or 'TRY').upper() in ('TRY', 'TL'):
        return amount  # a rate on a TL invoice is informational only
    if amount_try:
        return amount_try
    return amount * rate if rate else None


def _month_start(date):
    return date.replace(day=1)


class GuvenKaynakKarsilastirma(models.Model):
    _name = 'guven.kaynak.karsilastirma'
    _description = 'HBYS Sistemleri Karşılaştırması'
    _order = 'issue_date desc, id desc'
    _rec_name = 'invoice_number'

    # Session advisory lock; 737001-737004 are taken by guven_fatura_analiz.
    _LOCK_ID = 737010
    TOUR_BUDGET = 300
    CHUNK_SIZE = 500
    RECENT_DAYS = 45
    DATE_MISMATCH_MARGIN_DAYS = 92
    CEREBRUM_SHRINK_LIMIT = 0.8
    PUSULA_EMPTY_GUARD = 100

    # --- izibiz snapshot ---
    fatura_id = fields.Many2one(
        'guven.fatura', string='izibiz Faturası', required=True, index=True, ondelete='cascade',
    )
    company_id = fields.Many2one('res.company', string='Şirket', required=True, index=True)
    direction = fields.Selection([('IN', 'Gelen'), ('OUT', 'Giden')], string='Yön', index=True)
    gib_source = fields.Selection(
        [('e-fatura-izibiz', 'E-Fatura'), ('e-arsiv-izibiz', 'E-Arşiv')], string='Belge Türü',
    )
    invoice_number = fields.Char(string='Fatura No', index=True)
    issue_date = fields.Date(string='Fatura Tarihi', index=True)
    amount_try = fields.Float(string='GİB Tutarı (KDV Dahil, TL)', digits=(16, 2))
    payable_amount_try = fields.Float(string='GİB Ödenecek (TL)', digits=(16, 2))
    counterparty_vkn = fields.Char(
        string='Gönderen VKN/TCKN', help="Yalnızca gelen faturalarda saklanır (kimlik doğrulaması için).",
    )
    counterparty_name = fields.Char(string='Karşı Taraf', compute='_compute_counterparty_name')
    logo_match_count = fields.Integer(string='Logo Eşleşme Sayısı')
    gib_write_date = fields.Datetime(string='izibiz Kaydı Değişikliği')

    # --- Pusula ---
    pusula_status = fields.Selection(SOURCE_STATUS, string='Pusula Durumu', index=True)
    pusula_candidate_count = fields.Integer(string='Pusula Aday Sayısı')
    pusula_record_id = fields.Integer(string='Pusula Fatura Id')
    pusula_number = fields.Char(string='Pusula Fatura No')
    pusula_date = fields.Date(string='Pusula Tarihi')
    pusula_type = fields.Selection(PUSULA_TYPES, string='Pusula Fatura Tipi')
    pusula_unit = fields.Integer(string='Pusula Şube')
    pusula_amount = fields.Float(string='Pusula Tutarı (TL)', digits=(16, 2))
    pusula_vkn = fields.Char(string='Pusula VKN/TCKN')
    pusula_amount_diff = fields.Float(string='Pusula Tutar Farkı', digits=(16, 2))
    pusula_amount_level = fields.Selection(AMOUNT_LEVEL, string='Pusula Tutar Uyumu')
    pusula_amount_basis = fields.Selection(AMOUNT_BASIS, string='Pusula Tutar Tabanı')
    pusula_identity_match = fields.Selection(MATCH, string='Pusula Kimlik Uyumu')
    pusula_ettn_match = fields.Selection(MATCH, string='Pusula ETTN Uyumu')
    pusula_invalid_reason = fields.Selection(INVALID_REASON, string='Pusula Geçersizlik Nedeni')
    pusula_document_kind = fields.Integer(string='Pusula Evrak Tipi')
    pusula_other_company = fields.Boolean(string='Pusula: Başka Şirkette Var')
    pusula_candidates_note = fields.Char(string='Pusula Adayları')
    pusula_signature = fields.Char(string='Pusula İmzası', readonly=True)

    # --- Cerebrum ---
    cerebrum_status = fields.Selection(SOURCE_STATUS, string='Cerebrum Durumu', index=True)
    cerebrum_candidate_count = fields.Integer(string='Cerebrum Aday Sayısı')
    cerebrum_record_id = fields.Integer(string='Cerebrum Fatura Id')
    cerebrum_number = fields.Char(string='Cerebrum Fatura No')
    cerebrum_date = fields.Date(string='Cerebrum Tarihi')
    cerebrum_type = fields.Selection(CEREBRUM_TYPES, string='Cerebrum Fiş Tipi')
    cerebrum_unit = fields.Integer(string='Cerebrum Şirket')
    cerebrum_amount = fields.Float(string='Cerebrum Tutarı (TL)', digits=(16, 2))
    cerebrum_vkn = fields.Char(string='Cerebrum VKN')
    cerebrum_amount_diff = fields.Float(string='Cerebrum Tutar Farkı', digits=(16, 2))
    cerebrum_amount_level = fields.Selection(AMOUNT_LEVEL, string='Cerebrum Tutar Uyumu')
    cerebrum_amount_basis = fields.Selection(AMOUNT_BASIS, string='Cerebrum Tutar Tabanı')
    cerebrum_identity_match = fields.Selection(MATCH, string='Cerebrum Kimlik Uyumu')
    cerebrum_invalid_reason = fields.Selection(INVALID_REASON, string='Cerebrum Geçersizlik Nedeni')
    cerebrum_other_company = fields.Boolean(string='Cerebrum: Başka Şirkette Var')
    cerebrum_candidates_note = fields.Char(string='Cerebrum Adayları')
    cerebrum_signature = fields.Char(string='Cerebrum İmzası', readonly=True)

    # --- Summary ---
    overall_status = fields.Selection(
        OVERALL_STATUS, string='Genel Durum', compute='_compute_overall_status', store=True, index=True,
    )
    gib_signature = fields.Char(string='izibiz İmzası', readonly=True)
    evaluated_at = fields.Datetime(string='Son Hüküm Değişikliği', readonly=True)

    _unique_fatura = models.Constraint(
        'UNIQUE (fatura_id)',
        'Her izibiz faturası için tek karşılaştırma satırı olabilir.',
    )

    @api.depends('pusula_status', 'cerebrum_status')
    def _compute_overall_status(self):
        for record in self:
            p = record.pusula_status or UNCHECKED
            c = record.cerebrum_status or UNCHECKED
            if UNCHECKED in (p, c) and matching.FOUND not in (p, c):
                record.overall_status = UNCHECKED
            else:
                record.overall_status = matching.overall_status(
                    matching.SourceVerdict(status=p), matching.SourceVerdict(status=c),
                )

    @api.depends('direction', 'fatura_id')
    def _compute_counterparty_name(self):
        for record in self:
            invoice = record.fatura_id
            record.counterparty_name = invoice.sender_name if record.direction == 'IN' else invoice.receiver_name

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    @api.model
    def _cron_run(self):
        self._run_tour(commit=True)

    @api.model
    def action_run_now(self):
        if not self.env.user.has_group('base.group_system'):
            raise UserError(_("Karşılaştırmayı yalnızca sistem yöneticisi çalıştırabilir."))
        stats = self._run_tour(commit=True)
        if stats is None:
            raise UserError(_("Karşılaştırma şu anda başka bir işlem tarafından çalıştırılıyor."))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Karşılaştırma turu tamamlandı"),
                'message': _(
                    "%(months)s ay işlendi, %(created)s satır eklendi, %(updated)s satır güncellendi, "
                    "%(removed)s satır kaldırıldı.",
                    months=stats['months'], created=stats['created'],
                    updated=stats['updated'], removed=stats['removed'],
                ),
                'type': 'warning' if stats['source_errors'] else 'success',
                'sticky': bool(stats['source_errors']),
            },
        }

    @api.model
    def _run_tour(self, commit=False, budget=None, today=None):
        """One budgeted tour. Returns stats, or None when another tour holds the lock."""
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s)", (self._LOCK_ID,))
        if not self.env.cr.fetchone()[0]:
            _logger.info("[GUVEN-KAYNAK] Tur zaten çalışıyor, atlanıyor.")
            return None
        try:
            return self.sudo()._run_tour_locked(
                commit=commit,
                deadline=time.monotonic() + (budget or self.TOUR_BUDGET),
                today=today or fields.Date.context_today(self),
            )
        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (self._LOCK_ID,))

    # ------------------------------------------------------------------
    # Tour
    # ------------------------------------------------------------------

    def _run_tour_locked(self, commit, deadline, today):
        started = time.monotonic()
        stats = collections.Counter()
        Source = self.env['guven.kaynak.source']
        pusula_source, cerebrum_source = Source._get('pusula'), Source._get('cerebrum')
        start_date = self._start_date()
        scopes = self._company_scopes()
        if not scopes:
            _logger.warning("[GUVEN-KAYNAK] Hiçbir şirkette kaynak eşlemesi yok, tur atlandı.")
            return stats

        stats['removed'] = self._remove_out_of_scope(start_date, list(scopes), commit)

        cerebrum_index, cerebrum_error, cerebrum_count = self._load_cerebrum(cerebrum_source, start_date)

        pusula = None
        pusula_error = None
        pusula_count = 0
        unknown_types = collections.Counter()
        if not pusula_source or not pusula_source.has_credentials():
            pusula_error = _("Pusula bağlantı bilgileri eksik.")
        else:
            try:
                pusula = pusula_source._client().__enter__()
            except Exception as exc:
                pusula_error = _("Pusula bağlantısı kurulamadı: %s", exc)

        try:
            priority, full = self._plan_months(start_date, today, list(scopes))
            processed = set()

            def process(month):
                nonlocal pusula, pusula_error, pusula_count
                p_index = None
                invoices, snapshots = self._load_invoices(month, start_date, list(scopes))
                if pusula is not None and invoices:
                    try:
                        records = pusula.fetch_by_numbers({inv.number for inv in invoices})
                        if len(invoices) >= self.PUSULA_EMPTY_GUARD and not records:
                            raise _SourceFailure(_("Pusula %s faturası için hiç kayıt döndürmedi.", len(invoices)))
                        p_index = matching.build_index(records)
                        pusula_count += len(records)
                        unknown_types.update(pusula.unknown_types)
                        pusula.unknown_types.clear()
                    except Exception as exc:
                        pusula_error = _("Pusula okuması başarısız: %s", exc)
                        _logger.warning("[GUVEN-KAYNAK] %s", pusula_error)
                        self._close(pusula)
                        pusula = None
                created, updated = self._apply_month(invoices, snapshots, scopes, p_index, cerebrum_index, commit)
                stats['created'] += created
                stats['updated'] += updated
                stats['months'] += 1
                processed.add(month)

            for month in priority:
                if time.monotonic() > deadline:
                    stats['budget_exhausted'] = 1
                    break
                process(month)

            if not stats['budget_exhausted'] and full is not None:
                finished = True
                for month in full:
                    if time.monotonic() > deadline:
                        stats['budget_exhausted'] = 1
                        finished = False
                        break
                    if month not in processed:
                        process(month)
                    self._set_param(FULL_SCAN_CURSOR_PARAM, (month + relativedelta(months=1)).isoformat())
                if finished:
                    self._set_param(FULL_SCAN_DATE_PARAM, today.isoformat())
                    self._set_param(FULL_SCAN_CURSOR_PARAM, False)
        finally:
            self._close(pusula)

        now = fields.Datetime.now()
        self._record_source_state(pusula_source, pusula_error, pusula_count, now, unknown_types=unknown_types)
        self._record_source_state(cerebrum_source, cerebrum_error, cerebrum_count, now, baseline=True)
        stats['source_errors'] = int(bool(pusula_error)) + int(bool(cerebrum_error))
        self._maybe_commit(commit)
        _logger.info(
            "[GUVEN-KAYNAK] Tur bitti: %s ay, %s yeni, %s güncel, %s kaldırıldı, %.1f sn%s%s",
            stats['months'], stats['created'], stats['updated'], stats['removed'],
            time.monotonic() - started,
            " | Pusula hatası: %s" % pusula_error if pusula_error else "",
            " | Cerebrum hatası: %s" % cerebrum_error if cerebrum_error else "",
        )
        return stats

    @staticmethod
    def _close(client):
        if client is not None:
            try:
                client.__exit__(None, None, None)
            except Exception:  # closing a broken connection must not mask the real error
                _logger.debug("[GUVEN-KAYNAK] Bağlantı kapatılamadı", exc_info=True)

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def _start_date(self):
        value = self.env['ir.config_parameter'].sudo().get_param(START_DATE_PARAM, DEFAULT_START_DATE)
        return fields.Date.to_date(value)

    def _set_param(self, key, value):
        self.env['ir.config_parameter'].sudo().set_param(key, value)

    def _company_scopes(self):
        scopes = {}
        for company in self.env['res.company'].sudo().search([]):
            pusula_units, cerebrum_units = company._kaynak_scope()
            if pusula_units or cerebrum_units:
                scopes[company.id] = (pusula_units, cerebrum_units)
        return scopes

    def _plan_months(self, start_date, today, company_ids):
        """(priority months, full-scan months or None).

        Priority: months touching the last RECENT_DAYS, then months with new,
        changed or unchecked rows. The daily full pass resumes from its
        cursor; once it completes, the date is stored and the list is None
        for the rest of the day.
        """
        first = _month_start(start_date)
        last = _month_start(today)
        months = []
        month = first
        while month <= last:
            months.append(month)
            month += relativedelta(months=1)

        recent_from = today - datetime.timedelta(days=self.RECENT_DAYS)
        priority = [m for m in reversed(months) if m + relativedelta(months=1) > recent_from]

        self.env.flush_all()
        self.env.cr.execute("""
            SELECT DISTINCT date_trunc('month', f.issue_date)::date
              FROM guven_fatura f
              LEFT JOIN guven_kaynak_karsilastirma k ON k.fatura_id = f.id
             WHERE f.gvn_active AND f.kaynak = ANY(%s) AND f.issue_date >= %s
               AND f.issue_date <= %s AND f.company_id = ANY(%s)
               AND (k.id IS NULL OR f.write_date > k.gib_write_date
                    OR k.pusula_status = %s OR k.cerebrum_status = %s)
             ORDER BY 1 DESC
        """, (IZIBIZ_SOURCES, start_date, today, company_ids, UNCHECKED, UNCHECKED))
        for (month,) in self.env.cr.fetchall():
            if month not in priority:
                priority.append(month)

        params = self.env['ir.config_parameter'].sudo()
        if params.get_param(FULL_SCAN_DATE_PARAM) == today.isoformat():
            return priority, None
        cursor = fields.Date.to_date(params.get_param(FULL_SCAN_CURSOR_PARAM) or False) or first
        cursor = max(cursor, first)
        return priority, [m for m in months if m >= cursor]

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def _load_cerebrum(self, source, start_date):
        """(index, error, count). A failed or shrunken read returns index None."""
        if not source or not source.has_credentials():
            return None, _("Cerebrum bağlantı bilgileri eksik."), 0
        date_from = start_date - datetime.timedelta(days=self.DATE_MISMATCH_MARGIN_DAYS)
        try:
            with source._client() as client:
                records = client.fetch_since(date_from)
        except Exception as exc:
            _logger.warning("[GUVEN-KAYNAK] Cerebrum okuması başarısız: %s", exc)
            return None, _("Cerebrum okuması başarısız: %s", exc), 0
        baseline = source.baseline_record_count
        if baseline and len(records) < baseline * self.CEREBRUM_SHRINK_LIMIT:
            error = _(
                "Cerebrum %(now)s kayıt döndürdü, önceki başarılı tur %(before)s kayıttı. "
                "Küçülme %%20'yi aştığı için Cerebrum sütunları yazılmadı.",
                now=len(records), before=baseline,
            )
            return None, error, len(records)
        return matching.build_index(records), None, len(records)

    def _load_invoices(self, month, start_date, company_ids):
        """Valid izibiz invoices of one month: (GibInvoice list, snapshot vals by id)."""
        date_from = max(month, start_date)
        date_to = month + relativedelta(months=1)
        self.env.flush_all()
        self.env.cr.execute("""
            SELECT id, company_id, direction, kaynak, invoice_id, issue_date, uuid, sender, receiver,
                   currency_code, exchange_rate, tax_inclusive_amount, tax_inclusive_amount_try,
                   payable_amount, payable_amount_try, COALESCE(logo_fatura_count, 0), write_date
              FROM guven_fatura
             WHERE gvn_active AND kaynak = ANY(%s) AND issue_date >= %s AND issue_date < %s
               AND company_id = ANY(%s)
        """, (IZIBIZ_SOURCES, date_from, date_to, company_ids))
        invoices, snapshots = [], {}
        for (rid, company, direction, kaynak, number, date, uuid, sender, receiver, currency, rate,
             incl, incl_try, pay, pay_try, logo_count, write_date) in self.env.cr.fetchall():
            amount = _try_amount(currency, rate, incl, incl_try)
            payable = _try_amount(currency, rate, pay, pay_try)
            invoices.append(matching.GibInvoice(
                invoice_id=rid, company_id=company, direction=direction, number=number, date=date,
                amount_try=amount, counterparty_identity=sender if direction == matching.IN else receiver,
                ettn=uuid, payable_amount_try=payable,
            ))
            snapshots[rid] = {
                'fatura_id': rid,
                'company_id': company,
                'direction': direction,
                'gib_source': kaynak,
                'invoice_number': number,
                'issue_date': date,
                'amount_try': amount or 0.0,
                'payable_amount_try': payable or 0.0,
                # TCKN of patients on outgoing e-Arşiv is not copied.
                'counterparty_vkn': (sender or False) if direction == matching.IN else False,
                'logo_match_count': logo_count,
            }
            snapshots[rid]['_write_date'] = write_date
        return invoices, snapshots

    def _existing_rows(self, invoice_ids):
        self.env.flush_all()
        self.env.cr.execute("""
            SELECT id, fatura_id, gib_signature, pusula_signature, cerebrum_signature, gib_write_date
              FROM guven_kaynak_karsilastirma WHERE fatura_id = ANY(%s)
        """, (invoice_ids,))
        return {row[1]: row for row in self.env.cr.fetchall()}

    # ------------------------------------------------------------------
    # Verdicts -> values
    # ------------------------------------------------------------------

    @staticmethod
    def _candidates_note(verdict):
        if verdict.status not in (matching.MULTIPLE, matching.DATE_MISMATCH, matching.INVALID):
            return False
        parts = []
        for candidate in sorted(verdict.candidates, key=lambda c: (c.date or datetime.date.min, c.record_id))[:5]:
            amount = '-' if candidate.amount_try is None else '%.2f' % candidate.amount_try
            suffix = '' if candidate.is_valid else ' (%s)' % candidate.invalid_reason
            parts.append('#%s %s %s TL%s' % (candidate.record_id, candidate.date or '-', amount, suffix))
        return '; '.join(parts)[:250] or False

    def _source_vals(self, prefix, verdict):
        record = verdict.record
        vals = {
            prefix + '_status': verdict.status,
            prefix + '_candidate_count': verdict.candidate_count,
            prefix + '_record_id': record.record_id if record else 0,
            prefix + '_number': (record.numbers[0] if record.numbers else False) if record else False,
            prefix + '_date': record.date if record else False,
            prefix + '_type': str(record.type_id) if record else False,
            prefix + '_unit': (record.unit_id or 0) if record else 0,
            prefix + '_amount': record.amount_try if record and record.amount_try is not None else 0.0,
            prefix + '_vkn': (record.identity or False) if record else False,
            prefix + '_amount_diff': verdict.amount_diff if verdict.amount_diff is not None else 0.0,
            prefix + '_amount_level': matching.amount_level(verdict) or False,
            prefix + '_amount_basis': verdict.amount_basis or False,
            prefix + '_identity_match': _MATCH_VALUE.get(verdict.identity_match, False),
            prefix + '_invalid_reason': (record.invalid_reason or False) if record else False,
            prefix + '_other_company': verdict.found_in_other_company,
            prefix + '_candidates_note': self._candidates_note(verdict),
        }
        if prefix == matching.PUSULA:
            vals['pusula_ettn_match'] = _MATCH_VALUE.get(verdict.ettn_match, False)
            vals['pusula_document_kind'] = (record.document_kind or 0) if record else 0
        return vals

    def _apply_month(self, invoices, snapshots, scopes, pusula_index, cerebrum_index, commit):
        existing = self._existing_rows([inv.invoice_id for inv in invoices])
        now = fields.Datetime.now()
        creates, updates = [], []
        for invoice in invoices:
            snapshot = dict(snapshots[invoice.invoice_id])
            write_date = snapshot.pop('_write_date')
            gib_sig = _signature(snapshot)
            pusula_scope, cerebrum_scope = scopes[invoice.company_id]
            parts = {}
            for prefix, index, scope in ((matching.PUSULA, pusula_index, pusula_scope),
                                         (matching.CEREBRUM, cerebrum_index, cerebrum_scope)):
                if index is not None:
                    vals = self._source_vals(prefix, matching.judge(invoice, index, scope))
                    parts[prefix] = (vals, _signature(vals))

            row = existing.get(invoice.invoice_id)
            if row is None:
                vals = dict(snapshot, gib_signature=gib_sig, gib_write_date=write_date, evaluated_at=now)
                for prefix in (matching.PUSULA, matching.CEREBRUM):
                    if prefix in parts:
                        part_vals, part_sig = parts[prefix]
                        vals.update(part_vals)
                        vals[prefix + '_signature'] = part_sig
                    else:
                        vals[prefix + '_status'] = UNCHECKED
                creates.append(vals)
                continue

            row_id, _fatura, old_gib, old_pusula, old_cerebrum, old_write_date = row
            vals = {}
            if old_gib != gib_sig:
                vals.update(snapshot, gib_signature=gib_sig)
            for prefix, old_sig in ((matching.PUSULA, old_pusula), (matching.CEREBRUM, old_cerebrum)):
                if prefix in parts and parts[prefix][1] != old_sig:
                    vals.update(parts[prefix][0])
                    vals[prefix + '_signature'] = parts[prefix][1]
            if vals:
                vals['evaluated_at'] = now
            if old_write_date != write_date:
                vals['gib_write_date'] = write_date
            if vals:
                updates.append((row_id, vals))

        self._write_chunks(creates, updates, commit)
        return len(creates), len(updates)

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def _write_chunks(self, creates, updates, commit):
        size = self.CHUNK_SIZE
        for start in range(0, len(creates), size):
            chunk = creates[start:start + size]
            self._with_retry(lambda chunk=chunk: self.create(chunk), commit)
        for start in range(0, len(updates), size):
            chunk = updates[start:start + size]

            def write_chunk(chunk=chunk):
                for row_id, vals in chunk:
                    self.browse(row_id).write(vals)
            self._with_retry(write_chunk, commit)

    def _with_retry(self, operation, commit, attempts=3):
        """One savepoint per chunk; retry on serialization conflicts."""
        for attempt in range(attempts):
            try:
                with self.env.cr.savepoint():
                    operation()
                break
            except _RETRYABLE:
                # The savepoint rolled back only this chunk; the transaction
                # is still usable, so no full rollback here.
                self.env.invalidate_all()
                if attempt == attempts - 1:
                    raise
                time.sleep(0.2 * 2 ** attempt)
        self._maybe_commit(commit)

    def _maybe_commit(self, commit):
        if commit:
            self.env.cr.commit()
            self.env.invalidate_all()

    def _remove_out_of_scope(self, start_date, company_ids, commit):
        """Drop rows whose izibiz invoice is no longer valid or in scope."""
        self.env.flush_all()
        self.env.cr.execute("""
            SELECT k.id FROM guven_kaynak_karsilastirma k
              JOIN guven_fatura f ON f.id = k.fatura_id
             WHERE NOT f.gvn_active OR f.kaynak <> ALL(%s) OR f.issue_date IS NULL
                OR f.issue_date < %s OR f.company_id <> ALL(%s)
        """, (IZIBIZ_SOURCES, start_date, company_ids))
        ids = [row[0] for row in self.env.cr.fetchall()]
        for start in range(0, len(ids), self.CHUNK_SIZE):
            chunk = ids[start:start + self.CHUNK_SIZE]
            self._with_retry(lambda chunk=chunk: self.browse(chunk).unlink(), commit)
        return len(ids)

    def _record_source_state(self, source, error, count, now, unknown_types=None, baseline=False):
        if not source:
            return
        vals = {'last_run_at': now, 'has_error': bool(error), 'last_error': error or False}
        if not error:
            vals.update(last_success_at=now, last_record_count=count)
            if baseline:
                vals['baseline_record_count'] = count
        if unknown_types is not None:
            vals['unknown_types'] = ', '.join(
                '%s: %s' % (type_id, n) for type_id, n in sorted(unknown_types.items())
            ) or False
        source.write(vals)
