from odoo import fields, models


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

    _unique_identifier = models.Constraint(
        'UNIQUE (identifier)',
        'Bu VKN/TCKN ile kayıt zaten mevcut.',
    )
