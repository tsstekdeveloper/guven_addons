from odoo import fields, models


class GuvenGibMukellef(models.Model):
    """GİB'e kayıtlı e-fatura/e-irsaliye mükellef kayıtları.

    izibiz SOAP GetGibUserList metodundan çekilir, read-only.
    """

    _name = 'guven.gib.mukellef'
    _description = 'GİB Mükellef Kaydı'
    _order = 'register_time desc, id desc'

    # === GetGibUserList USER alanları (birebir) ===
    identifier = fields.Char(
        string='VKN/TCKN',
        size=11,
        required=True,
        index=True,
        help='Mükellef VKN veya TCKN',
    )
    alias = fields.Char(
        string='Etiket',
        required=True,
        index=True,
        help='Gönderici veya posta kutusu etiketi '
             '(örn: urn:mail:defaultgb@firma.com)',
    )
    title = fields.Char(
        string='Ünvan',
        help="GİB'de kayıtlı şirket/kişi adı",
    )
    user_type = fields.Selection(
        selection=[
            ('OZEL', 'Özel'),
            ('KAMU', 'Kamu'),
        ],
        string='Tip',
    )
    unit = fields.Selection(
        selection=[
            ('GB', 'Gönderici'),
            ('PK', 'Posta Kutusu'),
        ],
        string='Birim',
    )
    document_type = fields.Selection(
        selection=[
            ('INVOICE', 'E-Fatura'),
            ('DESPATCHADVICE', 'E-İrsaliye'),
        ],
        string='Doküman Tipi',
        index=True,
    )
    register_time = fields.Datetime(
        string='Kayıt Tarihi',
        help='GİB ilk kayıt tarihi',
    )
    alias_creation_time = fields.Datetime(
        string='Etiket Oluşturma Tarihi',
    )
    deleted = fields.Boolean(
        string='Silinmiş',
        default=False,
        index=True,
    )
    deletion_time = fields.Datetime(
        string='Silme Tarihi',
    )

    # === Sync metadata ===
    last_synced_at = fields.Datetime(
        string='Son Sync Tarihi',
        readonly=True,
    )

    _unique_alias = models.Constraint(
        'UNIQUE (identifier, alias, document_type)',
        'Bu VKN + etiket + doküman tipi kombinasyonu zaten mevcut.',
    )
