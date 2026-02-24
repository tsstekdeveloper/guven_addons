import json
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class GuvenLogoSyncWizard(models.TransientModel):
    _name = 'guven.logo.sync.wizard'
    _description = 'Logo Fatura Senkronizasyonu'

    date_from = fields.Date(
        string='Başlangıç Tarihi', required=True, default=fields.Date.today,
    )
    date_to = fields.Date(
        string='Bitiş Tarihi', required=True, default=fields.Date.today,
    )
    company_ids = fields.Many2many(
        'res.company', string='Şirketler', readonly=True,
        default=lambda self: self.env.companies,
    )
    company_names = fields.Text(
        string='Senkronize Edilecek Şirketler', readonly=True,
        compute='_compute_company_names',
    )
    state = fields.Selection(
        [('draft', 'Bekliyor'), ('done', 'Tamamlandı')],
        string='Durum', default='draft',
    )
    total_created = fields.Integer(string='Yeni Kayıt', readonly=True)
    total_updated = fields.Integer(string='Güncellenen', readonly=True)
    total_fetched = fields.Integer(string='Logo Kayıt', readonly=True)
    total_deleted = fields.Integer(string='Silinen', readonly=True)
    log_messages = fields.Text(string='İşlem Logları', readonly=True)
    company_results = fields.Text(string='Şirket Sonuçları', readonly=True)
    match_total = fields.Integer(string='Taranan E-Fatura', readonly=True)
    match_single = fields.Integer(string='Tek Eşleşme', readonly=True)
    match_multi = fields.Integer(string='Çoklu Eşleşme', readonly=True)
    match_none = fields.Integer(string='Eşleşmeyen', readonly=True)
    match_tutar_farki = fields.Integer(string='Tutar Farkı', readonly=True)
    match_kimlik_farkli = fields.Integer(string='Kimlik Farklı', readonly=True)
    reverse_match_total = fields.Integer(string='Taranan Logo Fatura', readonly=True)
    reverse_match_single = fields.Integer(string='Tek Eşleşme (Ters)', readonly=True)
    reverse_match_multi = fields.Integer(string='Çoklu Eşleşme (Ters)', readonly=True)
    reverse_match_none = fields.Integer(string='Eşleşmeyen (Ters)', readonly=True)
    reverse_match_tutar_farki = fields.Integer(string='Tutar Farkı (Ters)', readonly=True)
    reverse_match_kimlik_farkli = fields.Integer(string='Kimlik Farklı (Ters)', readonly=True)
    report_html = fields.Html(
        string='Rapor', readonly=True,
        compute='_compute_report_html', sanitize=False,
    )

    @api.depends('company_ids')
    def _compute_company_names(self):
        for rec in self:
            rec.company_names = '\n'.join(rec.company_ids.mapped('name'))

    # ── Main Action ─────────────────────────────────────────────

    def action_sync(self):
        self.ensure_one()
        try:
            import pymssql  # noqa: F401
        except ImportError:
            raise UserError(_(
                "pymssql kütüphanesi yüklü değil. "
                "Lütfen 'pip install pymssql' komutuyla yükleyin."
            ))

        LogoFatura = self.env['guven.logo.fatura']
        log_lines = []
        totals = {'created': 0, 'updated': 0, 'fetched': 0, 'deleted': 0}
        company_data = []

        for company in self.company_ids:
            log_lines.append(f"--- {company.name} ---")

            if not company.has_logo_credentials():
                log_lines.append("  Logo MSSQL bilgileri tanımlı değil, atlandı.")
                company_data.append({
                    'name': company.name,
                    'fetched': 0, 'created': 0, 'updated': 0, 'deleted': 0,
                    'skipped': True,
                })
                continue

            try:
                with self.env.cr.savepoint():
                    result = LogoFatura._sync_company(
                        company, self.date_from, self.date_to,
                    )
            except Exception as e:
                _logger.exception("Logo sync error for %s", company.name)
                log_lines.append(f"  HATA: {e}")
                company_data.append({
                    'name': company.name,
                    'fetched': 0, 'created': 0, 'updated': 0, 'deleted': 0,
                    'error': str(e),
                })
                continue

            totals['fetched'] += result['fetched']
            totals['created'] += result['created']
            totals['updated'] += result['updated']
            totals['deleted'] += result['deleted']
            log_lines.append(
                f"  Logo'dan {result['fetched']} kayıt okundu: "
                f"{result['created']} yeni, {result['updated']} güncellenen, "
                f"{result['deleted']} silinen"
            )
            company_data.append({
                'name': company.name,
                **result,
            })

        # Logo Eşleştirme (GİB → Logo)
        match_stats = {}
        match_company_ids = [c.id for c in self.company_ids if c.has_logo_credentials()]
        if match_company_ids:
            log_lines.append("")
            log_lines.append("--- Logo Eşleştirme (GİB → Logo) ---")
            try:
                with self.env.cr.savepoint():
                    match_stats = self.env['guven.fatura']._match_logo_invoices(
                        self.date_from, self.date_to, match_company_ids,
                    ) or {}
                log_lines.append(
                    f"  Taranan: {match_stats.get('total', 0)}, "
                    f"Tek eşleşme: {match_stats.get('matched_single', 0)}, "
                    f"Çoklu: {match_stats.get('matched_multi', 0)}, "
                    f"Eşleşmeyen: {match_stats.get('unmatched', 0)}"
                )
            except Exception as e:
                _logger.exception("Logo matching error")
                log_lines.append(f"  Eşleştirme HATASI: {e}")

        # Ters Eşleştirme (Logo → GİB)
        reverse_match_stats = {}
        if match_company_ids:
            log_lines.append("")
            log_lines.append("--- Ters Eşleştirme (Logo → GİB) ---")
            try:
                with self.env.cr.savepoint():
                    reverse_match_stats = self.env['guven.logo.fatura']._match_gib_invoices(
                        self.date_from, self.date_to, match_company_ids,
                    ) or {}
                log_lines.append(
                    f"  Taranan: {reverse_match_stats.get('total', 0)}, "
                    f"Tek eşleşme: {reverse_match_stats.get('matched_single', 0)}, "
                    f"Çoklu: {reverse_match_stats.get('matched_multi', 0)}, "
                    f"Eşleşmeyen: {reverse_match_stats.get('unmatched', 0)}"
                )
            except Exception as e:
                _logger.exception("Reverse matching error")
                log_lines.append(f"  Ters eşleştirme HATASI: {e}")

        self.write({
            'state': 'done',
            'total_created': totals['created'],
            'total_updated': totals['updated'],
            'total_fetched': totals['fetched'],
            'total_deleted': totals['deleted'],
            'match_total': match_stats.get('total', 0),
            'match_single': match_stats.get('matched_single', 0),
            'match_multi': match_stats.get('matched_multi', 0),
            'match_none': match_stats.get('unmatched', 0),
            'match_tutar_farki': match_stats.get('tutar_farki', 0),
            'match_kimlik_farkli': match_stats.get('kimlik_farkli', 0),
            'reverse_match_total': reverse_match_stats.get('total', 0),
            'reverse_match_single': reverse_match_stats.get('matched_single', 0),
            'reverse_match_multi': reverse_match_stats.get('matched_multi', 0),
            'reverse_match_none': reverse_match_stats.get('unmatched', 0),
            'reverse_match_tutar_farki': reverse_match_stats.get('tutar_farki', 0),
            'reverse_match_kimlik_farkli': reverse_match_stats.get('kimlik_farkli', 0),
            'log_messages': '\n'.join(log_lines),
            'company_results': json.dumps(company_data, ensure_ascii=False),
        })

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_close(self):
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

    # ── HTML Report ─────────────────────────────────────────────

    @api.depends(
        'state', 'total_created', 'total_updated', 'total_fetched',
        'total_deleted', 'company_results', 'date_from', 'date_to',
        'match_total', 'match_single', 'match_multi', 'match_none',
        'match_tutar_farki', 'match_kimlik_farkli',
        'reverse_match_total', 'reverse_match_single', 'reverse_match_multi',
        'reverse_match_none', 'reverse_match_tutar_farki', 'reverse_match_kimlik_farkli',
    )
    def _compute_report_html(self):
        for rec in self:
            if rec.state != 'done':
                rec.report_html = False
                continue
            rec.report_html = rec._build_report_html()

    @staticmethod
    def _fmt(num):
        """Format number with Turkish thousands separator."""
        return f"{num:,}".replace(",", ".")

    def _build_report_html(self):
        company_data = json.loads(self.company_results or '[]')
        fmt = self._fmt

        date_from = self.date_from.strftime('%d.%m.%Y') if self.date_from else ''
        date_to = self.date_to.strftime('%d.%m.%Y') if self.date_to else ''

        # Build company rows
        company_rows = ''
        for cd in company_data:
            if cd.get('skipped'):
                company_rows += (
                    '<tr>'
                    f'<td class="sr-company">{cd["name"]}</td>'
                    '<td colspan="4" style="color:#94a3b8;text-align:center">'
                    'MSSQL bilgileri tanımlı değil</td>'
                    '</tr>'
                )
                continue
            if cd.get('error'):
                company_rows += (
                    '<tr>'
                    f'<td class="sr-company">{cd["name"]}</td>'
                    '<td colspan="4" style="color:#ef4444;text-align:center">'
                    f'Hata: {cd["error"][:80]}</td>'
                    '</tr>'
                )
                continue
            deleted_val = cd.get("deleted", 0)
            deleted_style = ' style="color:#ef4444"' if deleted_val else ''
            company_rows += (
                '<tr>'
                f'<td class="sr-company">{cd["name"]}</td>'
                f'<td>{fmt(cd["fetched"])}</td>'
                f'<td><strong>{fmt(cd["created"])}</strong></td>'
                f'<td>{fmt(cd["updated"])}</td>'
                f'<td{deleted_style}>{fmt(deleted_val)}</td>'
                '</tr>'
            )

        # Build matching section
        match_html = ''
        if self.match_total > 0:
            match_pct = (
                round(self.match_single / self.match_total * 100)
                if self.match_total else 0
            )

            # Anomaly rows
            anomaly_rows = ''
            anomalies = [
                (self.match_tutar_farki, 'Tutar Farkı', '#f59e0b',
                 'E-fatura ve Logo tutarları arasında fark var'),
                (self.match_kimlik_farkli, 'Kimlik Farklı', '#ef4444',
                 'GİB VKN/TCKN ile Logo VKN/TCKN uyumsuzluğu'),
            ]
            for count, label, color, desc in anomalies:
                if count > 0:
                    anomaly_rows += (
                        '<tr>'
                        f'<td style="text-align:left;padding-left:18px;font-weight:600">'
                        f'<span style="display:inline-block;width:8px;height:8px;'
                        f'border-radius:50%;background:{color};margin-right:8px"></span>'
                        f'{label}</td>'
                        f'<td><strong>{fmt(count)}</strong></td>'
                        f'<td style="color:#64748b;text-align:left">{desc}</td>'
                        '</tr>'
                    )

            anomaly_table = ''
            if anomaly_rows:
                anomaly_table = f"""\
<div class="sr-section-title" style="margin-top:16px">Uyumsuzluklar</div>
<table class="sr-table">
    <thead>
        <tr>
            <th>Tür</th>
            <th>Adet</th>
            <th>Açıklama</th>
        </tr>
    </thead>
    <tbody>
        {anomaly_rows}
    </tbody>
</table>"""

            match_html = f"""\
<div style="border-top:2px solid #e2e8f0;margin:24px 0 20px;padding-top:0"></div>

<div class="sr-header" style="background:linear-gradient(135deg,#0f172a 0%,#7c3aed 50%,#a855f7 100%)">
    <h2>Logo Eslestirme Sonuclari</h2>
    <div class="sr-date">{date_from} &mdash; {date_to}</div>
</div>

<div class="sr-cards">
    <div class="sr-card" style="border-color:#7c3aed">
        <div class="sr-card-label">Taranan E-Fatura</div>
        <div class="sr-card-num" style="color:#6d28d9">{fmt(self.match_total)}</div>
        <div class="sr-card-sub">fatura</div>
    </div>
    <div class="sr-card" style="border-color:#10b981">
        <div class="sr-card-label">Tek Eslesme</div>
        <div class="sr-card-num" style="color:#059669">{fmt(self.match_single)}</div>
        <div class="sr-card-sub">%{match_pct} basari</div>
    </div>
    <div class="sr-card" style="border-color:#f59e0b">
        <div class="sr-card-label">Coklu Eslesme</div>
        <div class="sr-card-num" style="color:#d97706">{fmt(self.match_multi)}</div>
        <div class="sr-card-sub">fatura</div>
    </div>
    <div class="sr-card" style="border-color:#ef4444">
        <div class="sr-card-label">Eslesmeyen</div>
        <div class="sr-card-num" style="color:#dc2626">{fmt(self.match_none)}</div>
        <div class="sr-card-sub">fatura</div>
    </div>
</div>

{anomaly_table}"""

        # Build reverse matching section (Logo → GİB)
        reverse_match_html = ''
        if self.reverse_match_total > 0:
            reverse_pct = (
                round(self.reverse_match_single / self.reverse_match_total * 100)
                if self.reverse_match_total else 0
            )

            # Anomaly rows
            reverse_anomaly_rows = ''
            reverse_anomalies = [
                (self.reverse_match_tutar_farki, 'Tutar Farkı', '#f59e0b',
                 'Logo ve GİB tutarları arasında fark var'),
                (self.reverse_match_kimlik_farkli, 'Kimlik Farklı', '#ef4444',
                 'Logo VKN/TCKN ile GİB VKN/TCKN uyumsuzluğu'),
            ]
            for count, label, color, desc in reverse_anomalies:
                if count > 0:
                    reverse_anomaly_rows += (
                        '<tr>'
                        f'<td style="text-align:left;padding-left:18px;font-weight:600">'
                        f'<span style="display:inline-block;width:8px;height:8px;'
                        f'border-radius:50%;background:{color};margin-right:8px"></span>'
                        f'{label}</td>'
                        f'<td><strong>{fmt(count)}</strong></td>'
                        f'<td style="color:#64748b;text-align:left">{desc}</td>'
                        '</tr>'
                    )

            reverse_anomaly_table = ''
            if reverse_anomaly_rows:
                reverse_anomaly_table = f"""\
<div class="sr-section-title" style="margin-top:16px">Uyumsuzluklar</div>
<table class="sr-table">
    <thead>
        <tr>
            <th>Tür</th>
            <th>Adet</th>
            <th>Açıklama</th>
        </tr>
    </thead>
    <tbody>
        {reverse_anomaly_rows}
    </tbody>
</table>"""

            reverse_match_html = f"""\
<div style="border-top:2px solid #e2e8f0;margin:24px 0 20px;padding-top:0"></div>

<div class="sr-header" style="background:linear-gradient(135deg,#1e3a5f 0%,#2563eb 50%,#3b82f6 100%)">
    <h2>Logo &rarr; GİB Ters Eslestirme Sonuclari</h2>
    <div class="sr-date">{date_from} &mdash; {date_to}</div>
</div>

<div class="sr-cards">
    <div class="sr-card" style="border-color:#2563eb">
        <div class="sr-card-label">Taranan Logo Fatura</div>
        <div class="sr-card-num" style="color:#1d4ed8">{fmt(self.reverse_match_total)}</div>
        <div class="sr-card-sub">fatura</div>
    </div>
    <div class="sr-card" style="border-color:#10b981">
        <div class="sr-card-label">Tek Eslesme</div>
        <div class="sr-card-num" style="color:#059669">{fmt(self.reverse_match_single)}</div>
        <div class="sr-card-sub">%{reverse_pct} basari</div>
    </div>
    <div class="sr-card" style="border-color:#f59e0b">
        <div class="sr-card-label">Coklu Eslesme</div>
        <div class="sr-card-num" style="color:#d97706">{fmt(self.reverse_match_multi)}</div>
        <div class="sr-card-sub">fatura</div>
    </div>
    <div class="sr-card" style="border-color:#ef4444">
        <div class="sr-card-label">Eslesmeyen</div>
        <div class="sr-card-num" style="color:#dc2626">{fmt(self.reverse_match_none)}</div>
        <div class="sr-card-sub">fatura</div>
    </div>
</div>

{reverse_anomaly_table}"""

        return f"""\
<div class="sr-wrap">
<style>
.sr-wrap {{
    font-family: Inter, 'Segoe UI', system-ui, -apple-system, sans-serif;
    color: #1e293b;
    line-height: 1.5;
}}
.sr-wrap * {{ box-sizing: border-box; }}

.sr-header {{
    background: linear-gradient(135deg, #0f172a 0%, #0369a1 50%, #0891b2 100%);
    color: #fff;
    padding: 28px 32px;
    border-radius: 16px;
    margin-bottom: 20px;
    position: relative;
    overflow: hidden;
}}
.sr-header::after {{
    content: '';
    position: absolute;
    top: -40px; right: -40px;
    width: 160px; height: 160px;
    border-radius: 50%;
    background: rgba(255,255,255,0.06);
}}
.sr-header h2 {{
    margin: 0 0 2px;
    font-size: 1.35em;
    font-weight: 700;
    letter-spacing: -0.02em;
}}
.sr-header .sr-date {{
    font-size: 0.85em;
    opacity: 0.75;
}}

.sr-cards {{
    display: flex;
    gap: 14px;
    margin-bottom: 16px;
}}
.sr-card {{
    flex: 1;
    background: #fff;
    border-radius: 12px;
    padding: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.07);
    border-top: 4px solid;
    text-align: center;
}}
.sr-card.c-fetch  {{ border-color: #64748b; }}
.sr-card.c-new    {{ border-color: #10b981; }}
.sr-card.c-upd    {{ border-color: #3b82f6; }}
.sr-card-label {{
    font-size: 0.7em;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #64748b;
    margin-bottom: 10px;
}}
.sr-card-num {{
    font-size: 2.2em;
    font-weight: 800;
    line-height: 1;
    margin-bottom: 6px;
}}
.sr-card.c-fetch .sr-card-num {{ color: #475569; }}
.sr-card.c-new   .sr-card-num {{ color: #059669; }}
.sr-card.c-upd   .sr-card-num {{ color: #2563eb; }}
.sr-card.c-del   .sr-card-num {{ color: #dc2626; }}
.sr-card-sub {{
    font-size: 0.8em;
    color: #94a3b8;
}}

.sr-section-title {{
    font-size: 0.82em;
    font-weight: 700;
    color: #475569;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 10px;
    margin-top: 8px;
    padding-left: 2px;
}}

.sr-table {{
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    overflow: hidden;
    font-size: 0.88em;
}}
.sr-table thead th {{
    background: #f1f5f9;
    padding: 11px 14px;
    font-size: 0.72em;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #64748b;
    text-align: center;
    border-bottom: 2px solid #e2e8f0;
}}
.sr-table thead th:first-child {{
    text-align: left;
    padding-left: 18px;
}}
.sr-table tbody td {{
    padding: 11px 14px;
    text-align: center;
    border-bottom: 1px solid #f1f5f9;
    vertical-align: middle;
}}
.sr-table td.sr-company {{
    text-align: left;
    font-weight: 600;
    color: #334155;
    padding-left: 18px;
}}
.sr-table tbody tr:last-child td {{
    border-bottom: none;
}}
.sr-table tbody tr:hover td {{
    background: #f8fafc;
}}
.sr-table tfoot td {{
    padding: 12px 14px;
    font-weight: 700;
    background: #f1f5f9;
    text-align: center;
    border-top: 2px solid #e2e8f0;
}}
.sr-table tfoot td:first-child {{
    text-align: left;
    padding-left: 18px;
}}
</style>

<div class="sr-header">
    <h2>Logo Fatura Senkronizasyon Raporu</h2>
    <div class="sr-date">{date_from} &mdash; {date_to}</div>
</div>

<div class="sr-cards">
    <div class="sr-card c-fetch">
        <div class="sr-card-label">Logo'dan Okunan</div>
        <div class="sr-card-num">{fmt(self.total_fetched)}</div>
        <div class="sr-card-sub">kayıt</div>
    </div>
    <div class="sr-card c-new">
        <div class="sr-card-label">Yeni Eklenen</div>
        <div class="sr-card-num">{fmt(self.total_created)}</div>
        <div class="sr-card-sub">kayıt</div>
    </div>
    <div class="sr-card c-upd">
        <div class="sr-card-label">Güncellenen</div>
        <div class="sr-card-num">{fmt(self.total_updated)}</div>
        <div class="sr-card-sub">kayıt</div>
    </div>
    <div class="sr-card c-del">
        <div class="sr-card-label">Silinen</div>
        <div class="sr-card-num">{fmt(self.total_deleted)}</div>
        <div class="sr-card-sub">kayıt</div>
    </div>
</div>

<div class="sr-section-title">Şirket Bazlı Detay</div>
<table class="sr-table">
    <thead>
        <tr>
            <th>Şirket</th>
            <th>Logo Kayıt</th>
            <th>Yeni</th>
            <th>Güncellenen</th>
            <th>Silinen</th>
        </tr>
    </thead>
    <tbody>
        {company_rows}
    </tbody>
    <tfoot>
        <tr>
            <td>TOPLAM</td>
            <td>{fmt(self.total_fetched)}</td>
            <td><strong>{fmt(self.total_created)}</strong></td>
            <td>{fmt(self.total_updated)}</td>
            <td{' style="color:#ef4444"' if self.total_deleted else ''}>{fmt(self.total_deleted)}</td>
        </tr>
    </tfoot>
</table>

{match_html}

{reverse_match_html}
</div>"""
