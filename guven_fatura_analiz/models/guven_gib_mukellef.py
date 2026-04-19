from odoo import _, api, fields, models


# Logo TRCODE → GİB yönü mapping'i
# Not: 10 (Alış Proforma), 11 (Satış Proforma), 12 (Nadir) mapping'e dahil değil
LOGO_IN_TRCODES = ['1', '3', '4', '5', '13']        # Gelen (Alış)
LOGO_OUT_TRCODES = ['2', '6', '7', '8', '9', '14']  # Giden (Satış)


class GuvenGibMukellef(models.Model):
    """Mükellef kayıtları. Kaynak: izibiz / GİB / LOGO."""

    _name = 'guven.gib.mukellef'
    _description = 'Mükellef Kaydı'
    _order = 'title, id'

    identifier = fields.Char(
        string='VKN/TCKN',
        size=11,
        required=True,
        index=True,
        help='Vergi Kimlik Numarası veya TC Kimlik Numarası',
    )
    title = fields.Char(
        string='Ünvan',
        help='Mükellef şirket/kişi adı',
    )
    kaynak = fields.Selection(
        selection=[
            ('izibiz', 'izibiz'),
            ('gib', 'GİB'),
            ('logo', 'LOGO'),
        ],
        string='Kaynak',
        required=True,
        default='izibiz',
        index=True,
    )
    tip = fields.Selection(
        selection=[
            ('firma', 'Firma'),
            ('sahis', 'Şahıs'),
        ],
        string='Tip',
        compute='_compute_tip',
        store=True,
        index=True,
        help='VKN (10 basamak) → Firma, TCKN (11 basamak) → Şahıs',
    )

    @api.depends('identifier')
    def _compute_tip(self):
        for rec in self:
            ident = (rec.identifier or '').strip()
            if len(ident) == 10:
                rec.tip = 'firma'
            elif len(ident) == 11:
                rec.tip = 'sahis'
            else:
                rec.tip = False

    @api.depends('title', 'identifier')
    def _compute_display_name(self):
        for rec in self:
            title = (rec.title or '').strip()
            ident = (rec.identifier or '').strip()
            if title and ident:
                rec.display_name = f'{title} ({ident})'
            elif title:
                rec.display_name = title
            elif ident:
                rec.display_name = ident
            else:
                rec.display_name = _('Mükellef #%s') % rec.id

    # ── Computed fatura ilişkileri (read-only, sayım + liste gösterimi) ──
    gib_fatura_gelen_ids = fields.Many2many(
        comodel_name='guven.fatura',
        string='GİB Faturaları (Gelen)',
        compute='_compute_fatura_iliskileri',
    )
    gib_fatura_giden_ids = fields.Many2many(
        comodel_name='guven.fatura',
        string='GİB Faturaları (Giden)',
        compute='_compute_fatura_iliskileri',
    )
    logo_fatura_gelen_ids = fields.Many2many(
        comodel_name='guven.logo.fatura',
        string='LOGO Faturaları (Gelen)',
        compute='_compute_fatura_iliskileri',
    )
    logo_fatura_giden_ids = fields.Many2many(
        comodel_name='guven.logo.fatura',
        string='LOGO Faturaları (Giden)',
        compute='_compute_fatura_iliskileri',
    )
    gib_gelen_count = fields.Integer(
        string='GİB Gelen Adet', compute='_compute_fatura_iliskileri',
    )
    gib_giden_count = fields.Integer(
        string='GİB Giden Adet', compute='_compute_fatura_iliskileri',
    )
    logo_gelen_count = fields.Integer(
        string='LOGO Gelen Adet', compute='_compute_fatura_iliskileri',
    )
    logo_giden_count = fields.Integer(
        string='LOGO Giden Adet', compute='_compute_fatura_iliskileri',
    )
    gib_gelen_total = fields.Float(
        string='GİB Gelen Toplam (TRY)', digits=(16, 2),
        compute='_compute_fatura_iliskileri',
    )
    gib_giden_total = fields.Float(
        string='GİB Giden Toplam (TRY)', digits=(16, 2),
        compute='_compute_fatura_iliskileri',
    )
    logo_gelen_total = fields.Float(
        string='LOGO Gelen Toplam (TRY)', digits=(16, 2),
        compute='_compute_fatura_iliskileri',
    )
    logo_giden_total = fields.Float(
        string='LOGO Giden Toplam (TRY)', digits=(16, 2),
        compute='_compute_fatura_iliskileri',
    )
    gib_gelen_perfect_total = fields.Float(
        string='GİB Gelen Tam Eşleşme Toplamı (TRY)', digits=(16, 2),
        compute='_compute_fatura_iliskileri',
    )
    gib_giden_perfect_total = fields.Float(
        string='GİB Giden Tam Eşleşme Toplamı (TRY)', digits=(16, 2),
        compute='_compute_fatura_iliskileri',
    )
    logo_gelen_perfect_total = fields.Float(
        string='LOGO Gelen Tam Eşleşme Toplamı (TRY)', digits=(16, 2),
        compute='_compute_fatura_iliskileri',
    )
    logo_giden_perfect_total = fields.Float(
        string='LOGO Giden Tam Eşleşme Toplamı (TRY)', digits=(16, 2),
        compute='_compute_fatura_iliskileri',
    )
    fatura_ozet_html = fields.Html(
        string='Fatura Özeti',
        compute='_compute_fatura_ozet_html',
        sanitize=False,
    )

    @api.depends('identifier')
    def _compute_fatura_iliskileri(self):
        GibFatura = self.env['guven.fatura']
        LogoFatura = self.env['guven.logo.fatura']
        for rec in self:
            ident = (rec.identifier or '').strip()
            if not ident:
                rec.gib_fatura_gelen_ids = False
                rec.gib_fatura_giden_ids = False
                rec.logo_fatura_gelen_ids = False
                rec.logo_fatura_giden_ids = False
                rec.gib_gelen_count = 0
                rec.gib_giden_count = 0
                rec.logo_gelen_count = 0
                rec.logo_giden_count = 0
                rec.gib_gelen_total = 0.0
                rec.gib_giden_total = 0.0
                rec.logo_gelen_total = 0.0
                rec.logo_giden_total = 0.0
                rec.gib_gelen_perfect_total = 0.0
                rec.gib_giden_perfect_total = 0.0
                rec.logo_gelen_perfect_total = 0.0
                rec.logo_giden_perfect_total = 0.0
                continue
            # GİB gelen: direction=IN AND sender=identifier
            gib_gelen = GibFatura.search([
                ('direction', '=', 'IN'),
                ('sender', '=', ident),
                ('gvn_active', '=', True),
            ])
            # GİB giden: direction=OUT AND receiver=identifier
            gib_giden = GibFatura.search([
                ('direction', '=', 'OUT'),
                ('receiver', '=', ident),
                ('gvn_active', '=', True),
            ])
            # LOGO gelen (alış TRCODE'lar)
            logo_gelen = LogoFatura.search([
                '|', ('vkn', '=', ident), ('tckn', '=', ident),
                ('fatura_tipi', 'in', LOGO_IN_TRCODES),
            ])
            # LOGO giden (satış TRCODE'lar)
            logo_giden = LogoFatura.search([
                '|', ('vkn', '=', ident), ('tckn', '=', ident),
                ('fatura_tipi', 'in', LOGO_OUT_TRCODES),
            ])
            rec.gib_fatura_gelen_ids = gib_gelen
            rec.gib_fatura_giden_ids = gib_giden
            rec.logo_fatura_gelen_ids = logo_gelen
            rec.logo_fatura_giden_ids = logo_giden
            rec.gib_gelen_count = len(gib_gelen)
            rec.gib_giden_count = len(gib_giden)
            rec.logo_gelen_count = len(logo_gelen)
            rec.logo_giden_count = len(logo_giden)
            rec.gib_gelen_total = sum(f.payable_amount_try or 0.0 for f in gib_gelen)
            rec.gib_giden_total = sum(f.payable_amount_try or 0.0 for f in gib_giden)
            rec.logo_gelen_total = sum(f.fatura_tutari or 0.0 for f in logo_gelen)
            rec.logo_giden_total = sum(f.fatura_tutari or 0.0 for f in logo_giden)
            rec.gib_gelen_perfect_total = sum(
                f.payable_amount_try or 0.0 for f in gib_gelen if f.perfect_fit
            )
            rec.gib_giden_perfect_total = sum(
                f.payable_amount_try or 0.0 for f in gib_giden if f.perfect_fit
            )
            rec.logo_gelen_perfect_total = sum(
                f.fatura_tutari or 0.0 for f in logo_gelen if f.perfect_fit
            )
            rec.logo_giden_perfect_total = sum(
                f.fatura_tutari or 0.0 for f in logo_giden if f.perfect_fit
            )

    @api.depends(
        'gib_gelen_count', 'gib_giden_count',
        'logo_gelen_count', 'logo_giden_count',
        'gib_gelen_total', 'gib_giden_total',
        'logo_gelen_total', 'logo_giden_total',
        'gib_gelen_perfect_total', 'gib_giden_perfect_total',
        'logo_gelen_perfect_total', 'logo_giden_perfect_total',
    )
    def _compute_fatura_ozet_html(self):
        for rec in self:
            rows = [
                ('GİB Gelen',  rec.gib_gelen_count,
                 rec.gib_gelen_total,  rec.gib_gelen_perfect_total),
                ('GİB Giden',  rec.gib_giden_count,
                 rec.gib_giden_total,  rec.gib_giden_perfect_total),
                ('LOGO Gelen', rec.logo_gelen_count,
                 rec.logo_gelen_total, rec.logo_gelen_perfect_total),
                ('LOGO Giden', rec.logo_giden_count,
                 rec.logo_giden_total, rec.logo_giden_perfect_total),
            ]

            def fmt_try(val):
                # Türkçe para formatı: 1.234.567,89
                s = f'{val:,.2f}'
                return s.replace(',', 'X').replace('.', ',').replace('X', '.')

            tr = (
                '<table style="border-collapse:collapse;width:100%;'
                'font-size:13px;">'
                '<thead>'
                '<tr style="background:#f0f0f0;text-align:left;">'
                '<th style="padding:4px 8px;border:1px solid #ddd;">Grup</th>'
                '<th style="padding:4px 8px;border:1px solid #ddd;'
                'text-align:right;">Adet</th>'
                '<th style="padding:4px 8px;border:1px solid #ddd;'
                'text-align:right;">Toplam (TRY)</th>'
                '<th style="padding:4px 8px;border:1px solid #ddd;'
                'text-align:right;">Tam Eşleşme (TRY)</th>'
                '<th style="padding:4px 8px;border:1px solid #ddd;'
                'text-align:right;">Oran %</th>'
                '</tr>'
                '</thead><tbody>'
            )
            for label, cnt, tot, perf in rows:
                oran = (100.0 * perf / tot) if tot else 0.0
                tr += (
                    f'<tr>'
                    f'<td style="padding:4px 8px;border:1px solid #ddd;">'
                    f'{label}</td>'
                    f'<td style="padding:4px 8px;border:1px solid #ddd;'
                    f'text-align:right;">{cnt}</td>'
                    f'<td style="padding:4px 8px;border:1px solid #ddd;'
                    f'text-align:right;">{fmt_try(tot)}</td>'
                    f'<td style="padding:4px 8px;border:1px solid #ddd;'
                    f'text-align:right;">{fmt_try(perf)}</td>'
                    f'<td style="padding:4px 8px;border:1px solid #ddd;'
                    f'text-align:right;">{oran:.1f}</td>'
                    f'</tr>'
                )
            tr += '</tbody></table>'
            rec.fatura_ozet_html = tr

    _unique_identifier = models.Constraint(
        'UNIQUE (identifier)',
        'Bu VKN/TCKN ile kayıt zaten mevcut.',
    )
