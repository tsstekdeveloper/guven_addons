from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import cerebrum_client, pusula_client


class GuvenKaynakSource(models.Model):
    """Connection settings and last-run state of one source system.

    Pusula and Cerebrum are single servers for the whole group, so their
    credentials live here instead of on every company. Only system
    administrators can read this model.
    """
    _name = 'guven.kaynak.source'
    _description = 'Kaynak Sistem Bağlantısı'
    _order = 'system'

    name = fields.Char(string='Ad', required=True)
    system = fields.Selection(
        [('pusula', 'Pusula (MSSQL)'), ('cerebrum', 'Cerebrum (Oracle)')],
        string='Sistem', required=True, readonly=True,
    )
    host = fields.Char(string='Sunucu')
    port = fields.Integer(string='Port')
    database = fields.Char(
        string='Veritabanı / Servis Adı',
        help="Pusula için veritabanı adı (PusulaHBYS), Cerebrum için Oracle servis adı (GVN).",
    )
    username = fields.Char(string='Kullanıcı')
    password = fields.Char(string='Parola', groups='base.group_system')

    last_run_at = fields.Datetime(string='Son Çalışma', readonly=True)
    last_success_at = fields.Datetime(string='Son Başarılı Okuma', readonly=True)
    has_error = fields.Boolean(string='Son Okuma Hatalı', readonly=True)
    last_error = fields.Text(string='Son Hata', readonly=True)
    last_record_count = fields.Integer(string='Son Okunan Kayıt', readonly=True)
    baseline_record_count = fields.Integer(
        string='Küçülme Koruması Tabanı', readonly=True,
        help="Cerebrum tam okumasında son başarılı turun kayıt sayısı. Yeni okuma bunun "
             "%80'inin altında kalırsa Cerebrum sütunları yazılmaz (bağlantı kopması koruması).",
    )
    unknown_types = fields.Char(
        string='Bilinmeyen Tipler', readonly=True,
        help="Okunan ama eşleme kurallarında karşılığı olmayan fatura tipi kodları ve sayıları.",
    )

    _unique_system = models.Constraint(
        'UNIQUE (system)',
        'Her kaynak sistem için tek bağlantı kaydı olabilir.',
    )

    @api.model
    def _get(self, system):
        return self.sudo().search([('system', '=', system)], limit=1)

    def has_credentials(self):
        self.ensure_one()
        source = self.sudo()
        return bool(source.host and source.port and source.database and source.username and source.password)

    def _client(self):
        self.ensure_one()
        source = self.sudo()
        if source.system == 'pusula':
            return pusula_client.PusulaClient(
                server=source.host, port=source.port, database=source.database,
                user=source.username, password=source.password,
            )
        return cerebrum_client.CerebrumClient(
            host=source.host, port=source.port, service_name=source.database,
            user=source.username, password=source.password,
        )

    def action_test_connection(self):
        self.ensure_one()
        if not self.has_credentials():
            raise UserError(_("Bağlantı bilgileri eksik."))
        try:
            with self._client() as client:
                client.ping()
        except Exception as exc:  # driver errors carry no credentials
            raise UserError(_("Bağlantı başarısız: %s", exc)) from exc
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Bağlantı başarılı"),
                'message': _("%s fatura tablosu okunabiliyor.", self.name),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_reset_baseline(self):
        """Allow one legitimate shrink, e.g. after moving the start date forward."""
        self.write({'baseline_record_count': 0})

    def action_run_comparison(self):
        return self.env['guven.kaynak.karsilastirma'].action_run_now()
