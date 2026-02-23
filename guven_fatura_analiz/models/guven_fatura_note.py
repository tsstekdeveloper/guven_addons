from odoo import fields, models


class GuvenFaturaNote(models.Model):
    _name = 'guven.fatura.note'
    _description = 'Fatura Notları'
    _order = 'sequence, id'

    fatura_id = fields.Many2one(
        'guven.fatura', string='Fatura', required=True,
        ondelete='cascade', index=True,
    )
    note_type = fields.Selection(
        [
            ('free_text', 'Serbest Metin'),
            ('hashtag', 'Hashtag'),
            ('lineext', 'Satır Uzantı'),
            ('meta', 'Meta'),
        ],
        string='Not Tipi',
    )
    key = fields.Char(string='Anahtar')
    value = fields.Text(string='Değer')
    sequence = fields.Integer(string='Sıra', default=10)
    company_id = fields.Many2one(
        related='fatura_id.company_id', store=True,
    )
