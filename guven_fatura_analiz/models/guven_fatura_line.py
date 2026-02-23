from odoo import fields, models


class GuvenFaturaLine(models.Model):
    _name = 'guven.fatura.line'
    _description = 'Fatura Kalemleri'
    _order = 'line_no'

    fatura_id = fields.Many2one(
        'guven.fatura', string='Fatura', required=True,
        ondelete='cascade', index=True,
    )
    line_no = fields.Integer(string='Satır No')
    item_name = fields.Char(string='Ürün/Hizmet Adı')
    quantity = fields.Float(string='Miktar', digits=(16, 8))
    line_extension_amount = fields.Float(
        string='Satır Tutarı (Döviz)', digits=(16, 8),
    )
    allowance_amount = fields.Float(
        string='İndirim Tutarı (Döviz)', digits=(16, 8),
    )
    currency_code = fields.Char(string='Para Birimi', size=3)

    # TRY — şimdilik plain field, sonra computed
    line_extension_amount_try = fields.Float(
        string='Satır Tutarı (TRY)', digits=(16, 8),
    )
    allowance_amount_try = fields.Float(
        string='İndirim Tutarı (TRY)', digits=(16, 8),
    )

    company_id = fields.Many2one(
        related='fatura_id.company_id', store=True,
    )
    tax_ids = fields.One2many(
        'guven.fatura.tax', 'line_id', string='Satır Vergileri',
    )
