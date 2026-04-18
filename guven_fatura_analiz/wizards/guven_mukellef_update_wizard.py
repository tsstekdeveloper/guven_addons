import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class GuvenMukellefUpdateWizard(models.TransientModel):
    """Mükellef kayıtlarını GİB fatura ve Logo fatura tablolarından besler."""

    _name = 'guven.mukellef.update.wizard'
    _description = 'Mükellef Kaydı Güncelleme'

    state = fields.Selection(
        [('draft', 'Bekliyor'), ('done', 'Tamamlandı')],
        string='Durum',
        default='draft',
    )

    # Sonuç sayaçları
    gib_unique_identifiers = fields.Integer(
        string='GİB Fatura Unique Kimlik',
        readonly=True,
    )
    gib_updated = fields.Integer(string='GİB Ünvan Güncelleme', readonly=True)
    gib_created = fields.Integer(string='GİB Yeni Eklenen', readonly=True)
    logo_unique_identifiers = fields.Integer(
        string='Logo Fatura Unique Kimlik',
        readonly=True,
    )
    logo_created = fields.Integer(string='Logo Yeni Eklenen', readonly=True)

    log_messages = fields.Text(string='İşlem Logları', readonly=True)
    report_html = fields.Html(
        string='Rapor',
        compute='_compute_report_html',
        sanitize=False,
    )

    # ── Computed ──────────────────────────────────────────────────

    @api.depends(
        'state', 'gib_unique_identifiers', 'gib_updated', 'gib_created',
        'logo_unique_identifiers', 'logo_created',
    )
    def _compute_report_html(self):
        for rec in self:
            if rec.state != 'done':
                rec.report_html = False
                continue
            rec.report_html = rec._build_report_html()

    # ── Main Action ───────────────────────────────────────────────

    def action_execute(self):
        """Mükellef güncelleme işlemini başlat."""
        self.ensure_one()
        log_lines = []
        Mukellef = self.env['guven.gib.mukellef'].sudo()
        Sequence = self.env['ir.sequence'].sudo()

        # === ADIM 1: GİB fatura tablosundan mükellefler ===
        _logger.info("[GUVEN-MUKELLEF-UPDATE] GİB adımı başlatıldı")
        log_lines.append("=== ADIM 1: GİB Fatura Tablosu ===")

        # sender + receiver UNION (her identifier için en son ünvan)
        self.env.cr.execute("""
            SELECT t.identifier, MAX(t.name) AS title
            FROM (
                SELECT TRIM(sender) AS identifier, sender_name AS name
                FROM guven_fatura
                WHERE sender IS NOT NULL AND TRIM(sender) != ''
                  AND gvn_active = TRUE
                UNION ALL
                SELECT TRIM(receiver), receiver_name
                FROM guven_fatura
                WHERE receiver IS NOT NULL AND TRIM(receiver) != ''
                  AND gvn_active = TRUE
            ) t
            WHERE t.identifier IS NOT NULL AND t.identifier != ''
            GROUP BY t.identifier
        """)
        gib_rows = self.env.cr.fetchall()
        gib_unique = len(gib_rows)
        log_lines.append(f"Unique kimlik sayısı: {gib_unique}")

        # Mevcut mükellef map'i (identifier → record)
        existing_all = Mukellef.search([])
        existing_by_id = {m.identifier: m for m in existing_all}

        gib_updated = 0
        gib_created = 0
        to_create = []
        for identifier, title in gib_rows:
            identifier = (identifier or '').strip()
            if not identifier:
                continue
            title = (title or '').strip() or False
            existing = existing_by_id.get(identifier)
            if existing:
                # sadece ünvan değişmişse güncelle, kaynağa dokunma
                if title and (existing.title or '') != title:
                    existing.title = title
                    gib_updated += 1
            else:
                to_create.append({
                    'identifier': identifier,
                    'title': title,
                    'kaynak': 'gib',
                })

        if to_create:
            created_recs = Mukellef.create(to_create)
            gib_created = len(created_recs)
            # existing map'ini güncelle (Adım 2 için)
            for rec in created_recs:
                existing_by_id[rec.identifier] = rec

        log_lines.append(
            f"Sonuç: {gib_updated} ünvan güncellendi, {gib_created} yeni eklendi"
        )
        log_lines.append("")

        # === ADIM 2: Logo fatura tablosundan mükellefler ===
        _logger.info("[GUVEN-MUKELLEF-UPDATE] Logo adımı başlatıldı")
        log_lines.append("=== ADIM 2: Logo Fatura Tablosu ===")

        self.env.cr.execute("""
            SELECT DISTINCT identifier FROM (
                SELECT TRIM(vkn) AS identifier FROM guven_logo_fatura
                WHERE vkn IS NOT NULL AND TRIM(vkn) != ''
                UNION
                SELECT TRIM(tckn) FROM guven_logo_fatura
                WHERE tckn IS NOT NULL AND TRIM(tckn) != ''
            ) t
            WHERE t.identifier IS NOT NULL AND t.identifier != ''
        """)
        logo_identifiers = {row[0] for row in self.env.cr.fetchall() if row[0]}
        logo_unique = len(logo_identifiers)
        log_lines.append(f"Unique kimlik sayısı: {logo_unique}")

        logo_to_create = []
        for identifier in logo_identifiers:
            if identifier in existing_by_id:
                continue  # mevcut → pas geç
            sec_num = Sequence.next_by_code('guven.gib.mukellef.logo')
            if not sec_num:
                raise UserError(_(
                    "LOGO Mükellef sequence tanımlı değil "
                    "(guven.gib.mukellef.logo)."
                ))
            logo_to_create.append({
                'identifier': identifier,
                'title': f'{sec_num} Nolu LOGO mükellefi',
                'kaynak': 'logo',
            })

        logo_created = 0
        if logo_to_create:
            created_recs = Mukellef.create(logo_to_create)
            logo_created = len(created_recs)

        log_lines.append(f"Sonuç: {logo_created} yeni Logo mükellefi eklendi")
        log_lines.append("")

        # Final
        total_created = gib_created + logo_created
        log_lines.append(
            f"GENEL: {total_created} yeni kayıt, {gib_updated} ünvan güncellendi"
        )

        self.write({
            'state': 'done',
            'gib_unique_identifiers': gib_unique,
            'gib_updated': gib_updated,
            'gib_created': gib_created,
            'logo_unique_identifiers': logo_unique,
            'logo_created': logo_created,
            'log_messages': '\n'.join(log_lines),
        })

        _logger.info(
            "[GUVEN-MUKELLEF-UPDATE] Tamamlandı: "
            "GİB %s yeni + %s günc, Logo %s yeni",
            gib_created, gib_updated, logo_created,
        )

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'context': {'dialog_size': 'large'},
        }

    def action_close(self):
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    # ── HTML Report ──────────────────────────────────────────────

    @staticmethod
    def _fmt(num):
        return f"{num:,}".replace(",", ".")

    def _build_report_html(self):
        fmt = self._fmt
        total_new = self.gib_created + self.logo_created
        return f"""\
<div style="font-family:Inter,'Segoe UI',system-ui,sans-serif;color:#1e293b;line-height:1.5">
<style>
.sr-header {{ background:linear-gradient(135deg,#0f172a 0%,#0369a1 50%,#0891b2 100%);
    color:#fff;padding:28px 32px;border-radius:16px;margin-bottom:20px; }}
.sr-header h2 {{ margin:0;font-size:1.35em;font-weight:700; }}
.sr-cards {{ display:flex;gap:14px;margin-bottom:16px; }}
.sr-card {{ flex:1;background:#fff;border-radius:12px;padding:20px;
    box-shadow:0 1px 3px rgba(0,0,0,0.07);border-top:4px solid;text-align:center; }}
.sr-card-label {{ font-size:0.7em;font-weight:700;text-transform:uppercase;
    letter-spacing:0.08em;color:#64748b;margin-bottom:10px; }}
.sr-card-num {{ font-size:2.2em;font-weight:800;line-height:1;margin-bottom:6px; }}
.sr-card-sub {{ font-size:0.8em;color:#94a3b8; }}
.c-new {{ border-color:#10b981; }} .c-new .sr-card-num {{ color:#059669; }}
.c-upd {{ border-color:#3b82f6; }} .c-upd .sr-card-num {{ color:#2563eb; }}
.c-gib {{ border-color:#7c3aed; }} .c-gib .sr-card-num {{ color:#6d28d9; }}
.c-logo {{ border-color:#f59e0b; }} .c-logo .sr-card-num {{ color:#d97706; }}
.sr-section-title {{ font-size:0.82em;font-weight:700;color:#475569;
    text-transform:uppercase;letter-spacing:0.06em;margin:16px 0 10px; }}
</style>

<div class="sr-header">
    <h2>Mükellef Güncelleme Raporu</h2>
    <div style="font-size:0.85em;opacity:0.75;margin-top:4px">
        Toplam {fmt(total_new)} yeni kayıt, {fmt(self.gib_updated)} ünvan güncellendi
    </div>
</div>

<div class="sr-section-title">GİB Fatura Tablosu</div>
<div class="sr-cards">
    <div class="sr-card c-gib">
        <div class="sr-card-label">Unique Kimlik</div>
        <div class="sr-card-num">{fmt(self.gib_unique_identifiers)}</div>
        <div class="sr-card-sub">sender+receiver</div>
    </div>
    <div class="sr-card c-new">
        <div class="sr-card-label">Yeni Eklenen</div>
        <div class="sr-card-num">{fmt(self.gib_created)}</div>
        <div class="sr-card-sub">kaynak=GİB</div>
    </div>
    <div class="sr-card c-upd">
        <div class="sr-card-label">Ünvan Güncellendi</div>
        <div class="sr-card-num">{fmt(self.gib_updated)}</div>
        <div class="sr-card-sub">mevcut kayıtta</div>
    </div>
</div>

<div class="sr-section-title">Logo Fatura Tablosu</div>
<div class="sr-cards">
    <div class="sr-card c-logo">
        <div class="sr-card-label">Unique Kimlik</div>
        <div class="sr-card-num">{fmt(self.logo_unique_identifiers)}</div>
        <div class="sr-card-sub">vkn+tckn</div>
    </div>
    <div class="sr-card c-new">
        <div class="sr-card-label">Yeni Eklenen</div>
        <div class="sr-card-num">{fmt(self.logo_created)}</div>
        <div class="sr-card-sub">kaynak=LOGO</div>
    </div>
</div>
</div>"""
