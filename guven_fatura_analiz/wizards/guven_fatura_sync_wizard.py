import json

from odoo import api, fields, models


class GuvenFaturaSyncWizard(models.TransientModel):
    _name = 'guven.fatura.sync.wizard'
    _description = 'izibiz E-Fatura Senkronizasyonu'

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
    efatura_in_created = fields.Integer(string='E-Fatura Gelen (Yeni)', readonly=True)
    efatura_in_updated = fields.Integer(string='E-Fatura Gelen (Güncellenen)', readonly=True)
    efatura_out_created = fields.Integer(string='E-Fatura Giden (Yeni)', readonly=True)
    efatura_out_updated = fields.Integer(string='E-Fatura Giden (Güncellenen)', readonly=True)
    earsiv_created = fields.Integer(string='E-Arşiv (Yeni)', readonly=True)
    earsiv_updated = fields.Integer(string='E-Arşiv (Güncellenen)', readonly=True)
    log_messages = fields.Text(string='İşlem Logları', readonly=True)
    company_results = fields.Text(string='Şirket Sonuçları', readonly=True)
    report_html = fields.Html(
        string='Rapor', readonly=True,
        compute='_compute_report_html', sanitize=False,
    )

    @api.depends('company_ids')
    def _compute_company_names(self):
        for rec in self:
            rec.company_names = '\n'.join(rec.company_ids.mapped('name'))

    def action_sync(self):
        self.ensure_one()
        Fatura = self.env['guven.fatura']
        log_lines = []
        totals = {
            'efatura_in_created': 0, 'efatura_in_updated': 0,
            'efatura_out_created': 0, 'efatura_out_updated': 0,
            'earsiv_created': 0, 'earsiv_updated': 0,
        }
        company_data = []

        for company in self.company_ids:
            log_lines.append(f"--- {company.name} ---")

            # E-Fatura Gelen (IN)
            result_in = Fatura._sync_efatura_headers(
                self.date_from, self.date_to, 'IN', company,
            )
            totals['efatura_in_created'] += result_in['created']
            totals['efatura_in_updated'] += result_in['updated']
            log_lines.append(
                f"  E-Fatura Gelen: {result_in['created']} yeni, "
                f"{result_in['updated']} güncellenen"
            )

            # E-Fatura Giden (OUT)
            result_out = Fatura._sync_efatura_headers(
                self.date_from, self.date_to, 'OUT', company,
            )
            totals['efatura_out_created'] += result_out['created']
            totals['efatura_out_updated'] += result_out['updated']
            log_lines.append(
                f"  E-Fatura Giden: {result_out['created']} yeni, "
                f"{result_out['updated']} güncellenen"
            )

            # E-Arşiv (always OUT)
            result_earsiv = Fatura._sync_earsiv_headers(
                self.date_from, self.date_to, company,
            )
            totals['earsiv_created'] += result_earsiv['created']
            totals['earsiv_updated'] += result_earsiv['updated']
            log_lines.append(
                f"  E-Arşiv: {result_earsiv['created']} yeni, "
                f"{result_earsiv['updated']} güncellenen"
            )

            company_data.append({
                'name': company.name,
                'efatura_in_created': result_in['created'],
                'efatura_in_updated': result_in['updated'],
                'efatura_out_created': result_out['created'],
                'efatura_out_updated': result_out['updated'],
                'earsiv_created': result_earsiv['created'],
                'earsiv_updated': result_earsiv['updated'],
            })

        self.write({
            'state': 'done',
            **totals,
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

    # ── HTML Report ──────────────────────────────────────────────

    @api.depends(
        'state', 'efatura_in_created', 'efatura_in_updated',
        'efatura_out_created', 'efatura_out_updated',
        'earsiv_created', 'earsiv_updated', 'company_results',
        'date_from', 'date_to',
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

    def _render_cell(self, new, upd):
        """Render a table cell with new/updated counts."""
        if new == 0 and upd == 0:
            return '<span style="color:#cbd5e1">—</span>'
        html = f'<strong>{self._fmt(new)}</strong>'
        if upd > 0:
            html += f'<br/><span class="sr-upd">+{self._fmt(upd)} günc.</span>'
        return html

    def _build_report_html(self):
        """Build the modern HTML sync report."""
        company_data = json.loads(self.company_results or '[]')
        fmt = self._fmt

        total_new = (
            self.efatura_in_created
            + self.efatura_out_created
            + self.earsiv_created
        )
        total_upd = (
            self.efatura_in_updated
            + self.efatura_out_updated
            + self.earsiv_updated
        )

        date_from = (
            self.date_from.strftime('%d.%m.%Y') if self.date_from else ''
        )
        date_to = (
            self.date_to.strftime('%d.%m.%Y') if self.date_to else ''
        )

        # Build company rows
        company_rows = ''
        for cd in company_data:
            c_new = (
                cd['efatura_in_created']
                + cd['efatura_out_created']
                + cd['earsiv_created']
            )
            c_upd = (
                cd['efatura_in_updated']
                + cd['efatura_out_updated']
                + cd['earsiv_updated']
            )
            company_rows += (
                '<tr>'
                f'<td class="sr-company">{cd["name"]}</td>'
                f'<td>{self._render_cell(cd["efatura_in_created"], cd["efatura_in_updated"])}</td>'
                f'<td>{self._render_cell(cd["efatura_out_created"], cd["efatura_out_updated"])}</td>'
                f'<td>{self._render_cell(cd["earsiv_created"], cd["earsiv_updated"])}</td>'
                f'<td class="sr-row-total">{self._render_cell(c_new, c_upd)}</td>'
                '</tr>'
            )

        efatura_in_upd_html = (
            f' · <b>+{fmt(self.efatura_in_updated)}</b> güncellenen'
            if self.efatura_in_updated else ''
        )
        efatura_out_upd_html = (
            f' · <b>+{fmt(self.efatura_out_updated)}</b> güncellenen'
            if self.efatura_out_updated else ''
        )
        earsiv_upd_html = (
            f' · <b>+{fmt(self.earsiv_updated)}</b> güncellenen'
            if self.earsiv_updated else ''
        )

        return f"""\
<div class="sr-wrap">
<style>
.sr-wrap {{
    font-family: Inter, 'Segoe UI', system-ui, -apple-system, sans-serif;
    color: #1e293b;
    line-height: 1.5;
}}
.sr-wrap * {{ box-sizing: border-box; }}

/* Header */
.sr-header {{
    background: linear-gradient(135deg, #0f172a 0%, #1e40af 50%, #7c3aed 100%);
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

/* Summary cards */
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
    position: relative;
    overflow: hidden;
}}
.sr-card::after {{
    content: '';
    position: absolute;
    bottom: -30px; right: -30px;
    width: 80px; height: 80px;
    border-radius: 50%;
    opacity: 0.06;
}}
.sr-card.c-in  {{ border-color: #10b981; }}
.sr-card.c-in::after  {{ background: #10b981; }}
.sr-card.c-out {{ border-color: #3b82f6; }}
.sr-card.c-out::after {{ background: #3b82f6; }}
.sr-card.c-ars {{ border-color: #8b5cf6; }}
.sr-card.c-ars::after {{ background: #8b5cf6; }}
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
.sr-card.c-in  .sr-card-num {{ color: #059669; }}
.sr-card.c-out .sr-card-num {{ color: #2563eb; }}
.sr-card.c-ars .sr-card-num {{ color: #7c3aed; }}
.sr-card-sub {{
    font-size: 0.8em;
    color: #94a3b8;
}}
.sr-card-sub b {{ color: #64748b; }}

/* Total bar */
.sr-total {{
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    padding: 14px 24px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 32px;
    margin-bottom: 24px;
}}
.sr-total-item {{ text-align: center; }}
.sr-total-num {{
    font-size: 1.6em;
    font-weight: 800;
    color: #0f172a;
}}
.sr-total-label {{
    font-size: 0.72em;
    color: #64748b;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}}
.sr-divider {{
    width: 1px;
    height: 36px;
    background: #e2e8f0;
}}

/* Section title */
.sr-section-title {{
    font-size: 0.82em;
    font-weight: 700;
    color: #475569;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 10px;
    padding-left: 2px;
}}

/* Company table */
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
    max-width: 200px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}}
.sr-table td.sr-row-total {{
    background: #fafbfc;
}}
.sr-table tbody tr:last-child td {{
    border-bottom: none;
}}
.sr-table tbody tr:hover td {{
    background: #f8fafc;
}}
.sr-table tbody tr:hover td.sr-row-total {{
    background: #f1f5f9;
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
.sr-upd {{
    font-size: 0.78em;
    color: #3b82f6;
    font-weight: 600;
}}
</style>

<div class="sr-header">
    <h2>Senkronizasyon Raporu</h2>
    <div class="sr-date">{date_from} &mdash; {date_to}</div>
</div>

<div class="sr-cards">
    <div class="sr-card c-in">
        <div class="sr-card-label">E-Fatura Gelen</div>
        <div class="sr-card-num">{fmt(self.efatura_in_created)}</div>
        <div class="sr-card-sub">yeni kayıt{efatura_in_upd_html}</div>
    </div>
    <div class="sr-card c-out">
        <div class="sr-card-label">E-Fatura Giden</div>
        <div class="sr-card-num">{fmt(self.efatura_out_created)}</div>
        <div class="sr-card-sub">yeni kayıt{efatura_out_upd_html}</div>
    </div>
    <div class="sr-card c-ars">
        <div class="sr-card-label">E-Arşiv</div>
        <div class="sr-card-num">{fmt(self.earsiv_created)}</div>
        <div class="sr-card-sub">yeni kayıt{earsiv_upd_html}</div>
    </div>
</div>

<div class="sr-total">
    <div class="sr-total-item">
        <div class="sr-total-num">{fmt(total_new)}</div>
        <div class="sr-total-label">Yeni Kayıt</div>
    </div>
    <div class="sr-divider"></div>
    <div class="sr-total-item">
        <div class="sr-total-num">{fmt(total_upd)}</div>
        <div class="sr-total-label">Güncellenen</div>
    </div>
    <div class="sr-divider"></div>
    <div class="sr-total-item">
        <div class="sr-total-num">{fmt(total_new + total_upd)}</div>
        <div class="sr-total-label">Toplam İşlem</div>
    </div>
</div>

<div class="sr-section-title">Şirket Bazlı Detay</div>
<table class="sr-table">
    <thead>
        <tr>
            <th>Şirket</th>
            <th>E-Fatura Gelen</th>
            <th>E-Fatura Giden</th>
            <th>E-Arşiv</th>
            <th>Toplam</th>
        </tr>
    </thead>
    <tbody>
        {company_rows}
    </tbody>
    <tfoot>
        <tr>
            <td>TOPLAM</td>
            <td>{self._render_cell(self.efatura_in_created, self.efatura_in_updated)}</td>
            <td>{self._render_cell(self.efatura_out_created, self.efatura_out_updated)}</td>
            <td>{self._render_cell(self.earsiv_created, self.earsiv_updated)}</td>
            <td>{self._render_cell(total_new, total_upd)}</td>
        </tr>
    </tfoot>
</table>
</div>"""
