from odoo import api, fields, models


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

    _unique_identifier = models.Constraint(
        'UNIQUE (identifier)',
        'Bu VKN/TCKN ile kayıt zaten mevcut.',
    )
