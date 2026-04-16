from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class GuvenLogoDonem(models.Model):
    """Logo firm code periods per company."""

    _name = 'guven.logo.donem'
    _description = 'Logo Firma Kodu Dönemleri'
    _order = 'baslangic_tarihi desc'

    company_id = fields.Many2one(
        'res.company',
        string='Şirket',
        required=True,
        ondelete='cascade',
    )
    logo_firma_kodu = fields.Char(
        string='Logo Firma Kodu',
        size=3,
        required=True,
        help='Logo ERP firma kodu (örn: 550, 600, 601)',
    )
    baslangic_tarihi = fields.Date(
        string='Başlangıç Tarihi',
        required=True,
    )
    bitis_tarihi = fields.Date(
        string='Bitiş Tarihi',
    )

    _unique_firma_kodu_company = models.Constraint(
        'UNIQUE(company_id, logo_firma_kodu)',
        'Aynı firma kodu bu şirket için zaten tanımlı.',
    )

    @api.constrains('baslangic_tarihi', 'bitis_tarihi')
    def _check_dates(self):
        for rec in self:
            if rec.bitis_tarihi and rec.baslangic_tarihi > rec.bitis_tarihi:
                raise ValidationError(
                    _("Bitiş tarihi başlangıç tarihinden önce olamaz.")
                )
