import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResCompany(models.Model):
    """Extend res.company with E-Fatura SOAP and Logo MSSQL settings."""

    _inherit = 'res.company'

    # ==================================================================
    # COMPUTED: GROUP-BASED EDITABILITY
    # ==================================================================
    can_edit_fatura_settings = fields.Boolean(
        compute='_compute_can_edit_fatura_settings',
    )

    @api.depends_context('uid')
    def _compute_can_edit_fatura_settings(self):
        can_edit = self.env.user.has_group(
            'guven_fatura_analiz.group_muhasebe_sorumlusu'
        )
        for record in self:
            record.can_edit_fatura_settings = can_edit

    # ==================================================================
    # E-FATURA SOAP FIELDS
    # ==================================================================
    efatura_username = fields.Char(
        string='E-Fatura Kullanıcı Adı',
        help='izibiz E-Fatura SOAP servisi kullanıcı adı',
    )
    efatura_password = fields.Char(
        string='E-Fatura Şifre',
        help='izibiz E-Fatura SOAP servisi şifresi',
    )
    efatura_ws = fields.Char(
        string='E-Fatura WS URL',
        default='https://efaturaws.izibiz.com.tr/EInvoiceWS?wsdl',
        help='E-Fatura SOAP Web Service URL',
    )
    efatura_earsiv_ws = fields.Char(
        string='E-Arşiv WS URL',
        default='https://earsivws.izibiz.com.tr/EIArchiveWS/EFaturaArchive?wsdl',
        help='E-Arşiv SOAP Web Service URL',
    )
    efatura_sync_lookback_days = fields.Integer(
        string='Geriye Dönük Güncelleme (Gün)',
        default=3,
        help='izibiz senkronizasyonunda, bugünden kaç gün geriye gidilerek '
             'mevcut kayıtlar güncellenecek. Örneğin 3 girilirse, '
             'son 3 günün faturaları her senkronizasyonda kontrol edilir.',
    )

    # ==================================================================
    # LOGO MSSQL FIELDS
    # ==================================================================
    logo_firma_kodu = fields.Char(
        string='Logo Firma Kodu',
        size=3,
        help='Logo ERP firma kodu (örn: 600, 601). '
             'SQL sorgularında LG_XXX_01_INVOICE gibi tablo isimlerinde kullanılır.',
    )
    logo_mssql_server = fields.Char(
        string='Logo MSSQL Sunucu',
        help='Logo veritabanı sunucu adresi veya IP',
    )
    logo_mssql_port = fields.Integer(
        string='Logo MSSQL Port',
        default=1433,
        help='Logo MSSQL sunucu portu',
    )
    logo_mssql_database = fields.Char(
        string='Logo Veritabanı',
        help='Logo veritabanı adı',
    )
    logo_mssql_username = fields.Char(
        string='Logo Kullanıcı',
        help='Logo veritabanı kullanıcı adı',
    )
    logo_mssql_password = fields.Char(
        string='Logo Şifre',
        help='Logo veritabanı şifresi',
    )
    logo_invoice_table = fields.Char(
        string='Logo Fatura Tablosu',
        help='Logo veritabanındaki fatura tablosu adı (örn: LG_600_01_INVOICE)',
    )
    logo_clcard_table = fields.Char(
        string='Logo Cari Bilgi Tablosu',
        help='Logo veritabanındaki cari hesap tablosu adı (örn: LG_600_CLCARD)',
    )
    logo_auto_sync = fields.Boolean(
        string='Otomatik Logo Sync',
        default=True,
        help='E-Fatura senkronizasyonu sonrası otomatik Logo kontrolü yapılsın mı?',
    )
    logo_sync_lookback_days = fields.Integer(
        string='Logo Geriye Dönüş (Gün)',
        default=30,
        help='Logo sync cron, bugünden kaç gün geriye gidecek.',
    )
    logo_sync_cursor_date = fields.Date(
        string='Logo Sync Cursor Tarihi',
        help='Logo sync cron bu tarihten itibaren devam edecek. '
             'Sistem tarafından otomatik yönetilir.',
    )
    logo_sync_last_completed_date = fields.Date(
        string='Son Tamamlanan Logo Sync',
        help='Logo sync cron en son bu tarih için tam bir tur tamamladı. '
             'Sistem tarafından otomatik yönetilir.',
    )

    # ==================================================================
    # CRON SYNC CURSOR
    # ==================================================================
    efatura_sync_cursor_date = fields.Date(
        string='Sync Cursor Tarihi',
        help='Header sync cron bu tarihten itibaren devam edecek. '
             'Sistem tarafından otomatik yönetilir.',
    )
    efatura_sync_last_completed_date = fields.Date(
        string='Son Tamamlanan Sync Tarihi',
        help='Header sync cron en son bu tarih için tam bir tur tamamladı. '
             'Yeni gün başladığında sıfırlanır. Sistem tarafından otomatik yönetilir.',
    )

    # ==================================================================
    # HELPER METHODS
    # ==================================================================
    def get_efatura_credentials(self):
        """Return E-Fatura SOAP credentials as a dict."""
        self.ensure_one()
        return {
            'username': self.efatura_username,
            'password': self.efatura_password,
            'ws_url': self.efatura_ws,
            'earsiv_ws_url': self.efatura_earsiv_ws,
        }

    def get_logo_credentials(self):
        """Return Logo MSSQL credentials as a dict."""
        self.ensure_one()
        return {
            'server': self.logo_mssql_server,
            'port': self.logo_mssql_port or 1433,
            'database': self.logo_mssql_database,
            'username': self.logo_mssql_username,
            'password': self.logo_mssql_password,
            'invoice_table': self.logo_invoice_table,
            'clcard_table': self.logo_clcard_table,
        }

    def has_efatura_credentials(self):
        """Check if E-Fatura SOAP credentials are configured."""
        self.ensure_one()
        return bool(self.efatura_username and self.efatura_password)

    def has_logo_credentials(self):
        """Check if Logo MSSQL credentials are configured."""
        self.ensure_one()
        return bool(
            self.logo_mssql_server
            and self.logo_mssql_database
            and self.logo_mssql_username
        )

    # ==================================================================
    # CONNECTION TEST BUTTONS
    # ==================================================================
    def action_test_efatura_connection(self):
        """Test E-Fatura SOAP connection using zeep."""
        self.ensure_one()
        if not self.has_efatura_credentials():
            raise UserError(_("E-Fatura kullanıcı adı ve şifre alanları doldurulmalıdır."))

        try:
            from zeep import Client
            from zeep.transports import Transport
            from requests import Session

            session = Session()
            session.verify = True
            transport = Transport(session=session, timeout=15)
            client = Client(self.efatura_ws, transport=transport)
            # Attempt a login call to verify credentials
            login_response = client.service.Login(
                REQUEST_HEADER={
                    'SESSION_ID': '',
                    'APPLICATION_NAME': 'Odoo',
                    'COMPRESSED': 'N',
                },
                USER_NAME=self.efatura_username,
                PASSWORD=self.efatura_password,
            )
            session_id = ''
            if hasattr(login_response, 'SESSION_ID'):
                session_id = login_response.SESSION_ID
            # Logout to clean up session
            if session_id:
                try:
                    client.service.Logout(
                        REQUEST_HEADER={'SESSION_ID': session_id},
                    )
                except Exception:
                    pass

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("Bağlantı Başarılı"),
                    'message': _("E-Fatura SOAP servisi ile bağlantı başarıyla kuruldu."),
                    'type': 'success',
                    'sticky': False,
                },
            }
        except ImportError:
            raise UserError(_(
                "zeep kütüphanesi yüklü değil. "
                "Lütfen 'pip install zeep' komutuyla yükleyin."
            ))
        except Exception as e:
            raise UserError(_("E-Fatura bağlantı hatası: %s") % str(e))

    def action_test_logo_connection(self):
        """Test Logo MSSQL connection using pymssql."""
        self.ensure_one()
        if not self.has_logo_credentials():
            raise UserError(_(
                "Logo MSSQL sunucu, veritabanı ve kullanıcı alanları doldurulmalıdır."
            ))

        try:
            import pymssql

            conn = pymssql.connect(
                server=self.logo_mssql_server,
                port=str(self.logo_mssql_port or 1433),
                user=self.logo_mssql_username,
                password=self.logo_mssql_password,
                database=self.logo_mssql_database,
                login_timeout=10,
                charset='cp1254',
            )
            cursor = conn.cursor()

            # Count invoices in the configured table
            invoice_count = 0
            if self.logo_invoice_table:
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {self.logo_invoice_table}")
                    invoice_count = cursor.fetchone()[0]
                except Exception:
                    pass

            conn.close()

            message = _("Logo MSSQL bağlantısı başarılı.")
            if self.logo_invoice_table and invoice_count:
                message = _(
                    "Logo MSSQL bağlantısı başarılı. "
                    "Fatura tablosunda %s kayıt bulundu."
                ) % invoice_count

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("Bağlantı Başarılı"),
                    'message': message,
                    'type': 'success',
                    'sticky': False,
                },
            }
        except ImportError:
            raise UserError(_(
                "pymssql kütüphanesi yüklü değil. "
                "Lütfen 'pip install pymssql' komutuyla yükleyin."
            ))
        except Exception as e:
            raise UserError(_("Logo MSSQL bağlantı hatası: %s") % str(e))
