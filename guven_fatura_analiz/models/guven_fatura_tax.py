from odoo import fields, models


class GuvenFaturaTax(models.Model):
    _name = 'guven.fatura.tax'
    _description = 'Fatura Vergi Detayları'
    _order = 'tax_type, percent'

    fatura_id = fields.Many2one(
        'guven.fatura', string='Fatura', required=True,
        ondelete='cascade', index=True,
    )
    line_id = fields.Many2one(
        'guven.fatura.line', string='Fatura Kalemi',
        ondelete='cascade',
    )
    tax_type = fields.Selection(
        [
            ('kdv', 'KDV'),
            ('withholding', 'Tevkifat'),
            ('bsmv', 'BSMV'),
            ('konaklama', 'Konaklama Vergisi'),
            ('tuketim', 'Tüketim Vergisi'),
            ('oiv', 'Özel İletişim Vergisi'),
            ('damga', 'Damga Vergisi'),
            ('diger', 'Diğer'),
        ],
        string='Vergi Tipi',
    )
    taxable_amount = fields.Float(string='Matrah (Döviz)', digits=(16, 8))
    tax_amount = fields.Float(string='Vergi Tutarı (Döviz)', digits=(16, 8))

    # TRY — şimdilik plain field, sonra computed
    taxable_amount_try = fields.Float(string='Matrah (TRY)', digits=(16, 8))
    tax_amount_try = fields.Float(
        string='Vergi Tutarı (TRY)', digits=(16, 8),
    )

    percent = fields.Float(string='Oran (%)', digits=(5, 2))
    currency_code = fields.Char(string='Para Birimi', size=3)
    company_id = fields.Many2one(
        related='fatura_id.company_id', store=True,
    )
