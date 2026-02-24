import base64
import io
import logging
import re
import time
import zipfile
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


PROFILE_ID_SELECTION = [
    ('TEMELFATURA', 'Temel Fatura'),
    ('TICARIFATURA', 'Ticari Fatura'),
    ('IHRACATFATURA', 'İhracat Faturası'),
    ('YOLCUBERABERFATURA', 'Yolcu Beraberi (tax-free)'),
    ('EARSIVFATURA', 'E-Arşiv Faturası'),
    ('ILAC_TIBBICIHAZ', 'İlaç ve Tıbbi Cihaz'),
    ('ENERJI', 'Enerji (EV Şarj)'),
    ('YATIRIMTESVIK', 'Yatırım Teşvik'),
    ('KONAKLAMAVERGISI', 'Konaklama Vergisi'),
    ('HKS', 'Hal Kayıt Sistemi'),
    ('KAMU', 'Kamu'),
    ('KAMUFATURA', 'Kamu Faturası'),
    ('SGK', 'SGK Faturası'),
    ('IDIS', 'İnşaat Demiri İzleme'),
    ('IPTAL', 'İptal'),
]

_VALID_PROFILE_IDS = {k for k, _ in PROFILE_ID_SELECTION}
_VALID_INVOICE_TYPE_CODES = set()  # populated after INVOICE_TYPE_CODE_SELECTION

INVOICE_TYPE_CODE_SELECTION = [
    ('SATIS', 'Normal Satış'),
    ('IADE', 'İade Faturası'),
    ('TEVKIFAT', 'KDV Tevkifatlı'),
    ('TEVKIFATIADE', 'Tevkifat İadesi'),
    ('ISTISNA', 'KDV İstisnalı'),
    ('OZELMATRAH', 'Özel Matrah'),
    ('IHRACKAYITLI', 'İhraç Kayıtlı'),
    ('IHRACAT', 'İhracat'),
    ('SARJ', 'EV Şarj (Haftalık)'),
    ('SARJANLIK', 'EV Şarj (Anlık)'),
    ('YTBSATIS', 'Yatırım Teşvik Satış'),
    ('YTBISTISNA', 'Yatırım Teşvik İstisna'),
    ('YTBIADE', 'Yatırım Teşvik İade'),
    ('KOMISYONCU', 'Hal Komisyoncusu'),
    ('SGK', 'SGK Faturası'),
]

_VALID_INVOICE_TYPE_CODES = {k for k, _ in INVOICE_TYPE_CODE_SELECTION}


class GuvenFatura(models.Model):
    _name = 'guven.fatura'
    _description = 'E-Fatura / E-Arşiv Kayıtları'
    _order = 'issue_date desc, invoice_id'
    _rec_name = 'invoice_id'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _check_company_auto = True

    # --- Temel Alanlar ---
    invoice_id = fields.Char(string='Fatura No', required=True, index=True)
    uuid = fields.Char(string='UUID', required=True, index=True)
    company_id = fields.Many2one(
        'res.company', string='Şirket', required=True,
        default=lambda self: self.env.company, ondelete='restrict', index=True,
    )
    kaynak = fields.Selection(
        [('e-fatura-izibiz', 'E-Fatura (izibiz)'),
         ('e-arsiv-izibiz', 'E-Arşiv (izibiz)'),
         ('e-arsiv-gibexcel', 'E-Arşiv (GİB Excel)'),
         ('e-fatura-qnbexcel', 'E-Fatura (QNB Excel)'),
         ('e-arsiv-qnbexcel', 'E-Arşiv (QNB Excel)')],
        string='Kaynak', required=True, index=True,
    )
    direction = fields.Selection(
        [('IN', 'Gelen'), ('OUT', 'Giden')],
        string='Yön', index=True,
    )
    profile_id = fields.Selection(PROFILE_ID_SELECTION, string='Fatura Profili')
    invoice_type_code = fields.Selection(
        INVOICE_TYPE_CODE_SELECTION, string='Fatura Tipi',
    )

    # --- Tarih/Saat ---
    issue_date = fields.Date(string='Fatura Tarihi')
    issue_time = fields.Char(string='Fatura Saati', size=8)

    # --- Taraf Bilgileri ---
    sender = fields.Char(string='Gönderen VKN/TCKN')
    sender_name = fields.Char(string='Gönderen Unvan')
    receiver = fields.Char(string='Alıcı VKN/TCKN')
    receiver_name = fields.Char(string='Alıcı Unvan')

    # --- Tutar Alanları (Döviz) ---
    currency_code = fields.Char(string='Para Birimi', size=3)
    exchange_rate = fields.Float(string='Döviz Kuru', digits=(16, 8))
    tax_exclusive_amount = fields.Float(
        string='Vergisiz Toplam (Döviz)', digits=(16, 8),
    )
    tax_inclusive_amount = fields.Float(
        string='Vergili Toplam (Döviz)', digits=(16, 8),
    )
    allowance_total_amount = fields.Float(
        string='İndirim Toplamı (Döviz)', digits=(16, 8),
    )
    payable_amount = fields.Float(
        string='Ödenecek Tutar (Döviz)', digits=(16, 8),
    )

    # --- Tutar Alanları (TRY) — şimdilik plain field, sonra computed ---
    tax_exclusive_amount_try = fields.Float(
        string='Vergisiz Toplam (TRY)', digits=(16, 8),
    )
    tax_inclusive_amount_try = fields.Float(
        string='Vergili Toplam (TRY)', digits=(16, 8),
    )
    allowance_total_amount_try = fields.Float(
        string='İndirim Toplamı (TRY)', digits=(16, 8),
    )
    payable_amount_try = fields.Float(
        string='Ödenecek Tutar (TRY)', digits=(16, 8),
    )

    # --- Durum ve Kontrol ---
    gvn_active = fields.Boolean(
        string='Geçerli', compute='_compute_gvn_active', store=True, index=True,
    )
    status_code = fields.Char(string='SOAP Durum Kodu')
    status_description = fields.Char(string='SOAP Durum Açıklaması')
    response_code = fields.Char(string='Yanıt Kodu')
    details_received = fields.Boolean(
        string='Detaylar Alındı', default=False, index=True,
        help='HEADER_ONLY=N ile XML detayları çekilip parse edildi mi?',
    )
    harici_iptal = fields.Boolean(string='Harici İptal', default=False)
    is_locked = fields.Boolean(string='Kilitli', default=False)
    locked_by_id = fields.Many2one(
        'res.users', string='Kilitleyen', readonly=True,
    )
    locked_date = fields.Datetime(string='Kilit Tarihi', readonly=True)
    lock_reason = fields.Text(string='Kilit Nedeni')

    # --- İptal İlişkisi ---
    is_cancellation = fields.Boolean(
        string='İptal Faturası', default=False, index=True,
    )
    cancelled_invoice_id = fields.Many2one(
        'guven.fatura', string='İptal Edilen Fatura', ondelete='set null',
    )
    cancellation_ids = fields.One2many(
        'guven.fatura', 'cancelled_invoice_id', string='İptal Faturaları',
        domain=[('is_cancellation', '=', True)],
    )

    # --- Logo Eşleşmesi ---
    logo_fatura_ids = fields.Many2many(
        'guven.logo.fatura',
        'guven_fatura_logo_fatura_rel',
        'fatura_id', 'logo_fatura_id',
        string='Logo Faturaları', copy=False,
    )
    logo_fatura_count = fields.Integer(string='Logo Eşleşen Kayıt', default=0, copy=False)
    logo_mssql_id = fields.Integer(string='Logo MSSQL ID', copy=False)
    logo_fatura_tarihi = fields.Date(string='Logo Fatura Tarihi', copy=False)
    logo_fatura_tutari = fields.Float(string='Logo Fatura Tutarı', digits=(16, 8), copy=False)
    logo_fatura_vkn = fields.Char(string='Logo VKN', size=11, copy=False)
    logo_fatura_tckn = fields.Char(string='Logo TCKN', size=11, copy=False)
    tutar_farki_var = fields.Boolean(string='Tutar Farkı Var', default=False, copy=False)
    tutar_farki = fields.Float(string='Tutar Farkı', digits=(16, 8), copy=False)
    kimlik_farkli = fields.Boolean(string='Kimlik Farklı', default=False, copy=False)
    fatura_tarihi_farkli = fields.Boolean(string='Tarih Farklı', default=False, copy=False)
    logo_karsilastirma_html = fields.Html(
        string='Karşılaştırma', compute='_compute_logo_karsilastirma_html',
        sanitize=False,
    )
    logo_notes = fields.Text(string='Logo Eşleşme Notları', copy=False)

    # --- One2many İlişkileri ---
    note_ids = fields.One2many(
        'guven.fatura.note', 'fatura_id', string='Notlar',
    )
    line_ids = fields.One2many(
        'guven.fatura.line', 'fatura_id', string='Kalemler',
    )
    tax_ids = fields.One2many(
        'guven.fatura.tax', 'fatura_id', string='Vergiler',
    )

    # --- Constraint ---
    _unique_invoice = models.Constraint(
        'UNIQUE (uuid, kaynak, company_id)',
        'Bu fatura zaten mevcut (aynı UUID, kaynak ve şirket).',
    )

    # --- Computed Methods ---

    # E-Fatura geçersiz durum kodları:
    #   116: izibiz Referans Kodu Değil (Muhtemel Geçersiz)
    #   120: Belge Ret Edildi
    #   130: Reddedildi  (DİKKAT: E-Arşiv'de 130 = "Raporlandı" = geçerli)
    #   136: Belge GİB'e Gönderilirken Hata Oluştu
    _EFATURA_INVALID_STATUS = frozenset(('116', '120', '130', '136'))

    # E-Arşiv geçersiz durum kodları:
    #   150: Raporlanmadan İptal Edildi
    #   200: Fatura ID Bulunamadı
    _EARSIV_INVALID_STATUS = frozenset(('150', '200'))

    @api.depends('status_code', 'kaynak', 'profile_id',
                 'is_cancellation', 'harici_iptal', 'cancellation_ids')
    def _compute_gvn_active(self):
        for record in self:
            # 0. Harici iptal — en yüksek öncelik
            if record.harici_iptal:
                record.gvn_active = False
                continue

            if record.kaynak and record.kaynak.startswith('e-arsiv'):
                # E-Arşiv: iptal kaydı, IPTAL profili, belirli status kodları
                # veya kendisine bağlı iptal kaydı varsa → geçersiz
                if record.is_cancellation:
                    record.gvn_active = False
                elif record.profile_id == 'IPTAL':
                    record.gvn_active = False
                elif record.status_code in self._EARSIV_INVALID_STATUS:
                    record.gvn_active = False
                elif record.cancellation_ids:
                    record.gvn_active = False
                else:
                    record.gvn_active = True
            else:
                # E-Fatura: sadece status_code bazlı
                record.gvn_active = record.status_code not in self._EFATURA_INVALID_STATUS

    @api.depends(
        'logo_fatura_count', 'logo_fatura_vkn', 'logo_fatura_tckn',
        'tutar_farki_var', 'tutar_farki', 'kimlik_farkli',
        'fatura_tarihi_farkli',
    )
    def _compute_logo_karsilastirma_html(self):
        for rec in self:
            if not rec.logo_fatura_count:
                rec.logo_karsilastirma_html = False
                continue
            rec.logo_karsilastirma_html = rec._build_karsilastirma_html()

    def _build_karsilastirma_html(self):
        """Build compact comparison badge HTML."""
        checks = []

        # Kimlik (VKN/TCKN birleşik)
        if self.kimlik_farkli:
            checks.append(('Kimlik', 'Farklı', '#ef4444', '#fef2f2'))
        else:
            logo_kimlik = self.logo_fatura_vkn or self.logo_fatura_tckn or '—'
            checks.append(('Kimlik', logo_kimlik, '#10b981', '#f0fdf4'))

        # Tutar farkı
        if self.tutar_farki_var:
            farki = f"{self.tutar_farki:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            checks.append(('Tutar Farkı', farki, '#f59e0b', '#fffbeb'))
        else:
            checks.append(('Tutar', 'Eşit', '#10b981', '#f0fdf4'))

        # Tarih
        if self.fatura_tarihi_farkli:
            checks.append(('Tarih', 'Farklı', '#ef4444', '#fef2f2'))
        else:
            checks.append(('Tarih', 'Eşit', '#10b981', '#f0fdf4'))

        badges = ''
        for label, value, color, bg in checks:
            icon = (
                '<span style="margin-right:4px">&#10005;</span>'
                if color in ('#ef4444', '#f59e0b')
                else '<span style="margin-right:4px">&#10003;</span>'
            )
            badges += (
                f'<span style="display:inline-flex;align-items:center;'
                f'padding:4px 10px;margin:3px 4px;border-radius:6px;'
                f'font-size:12px;font-weight:600;'
                f'background:{bg};color:{color};'
                f'border:1px solid {color}20">'
                f'{icon}'
                f'<span style="color:#64748b;font-weight:400;margin-right:4px">'
                f'{label}:</span> {value}</span>'
            )

        return (
            f'<div style="display:flex;flex-wrap:wrap;align-items:center;'
            f'gap:2px;padding:4px 0">{badges}</div>'
        )

    # ==================================================================
    # SOAP HELPERS
    # ==================================================================

    @api.model
    def _get_soap_client_and_login(self, company=None):
        """izibiz SOAP client oluştur ve login ol.

        Returns:
            tuple: (zeep_client, session_id, request_header_dict)
        """
        company = company or self.env.company
        if not company.has_efatura_credentials():
            raise UserError(
                _("%s için E-Fatura kullanıcı bilgileri tanımlanmamış.") % company.name
            )

        from zeep import Client
        from zeep.transports import Transport
        from zeep import Settings

        creds = company.get_efatura_credentials()

        session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(pool_connections=5, pool_maxsize=5, max_retries=retry)
        session.mount('https://', adapter)

        transport = Transport(session=session, timeout=90)
        settings = Settings(strict=False, xml_huge_tree=True)
        client = Client(creds['ws_url'], transport=transport, settings=settings)

        login_resp = client.service.Login(
            REQUEST_HEADER={'SESSION_ID': '-1', 'APPLICATION_NAME': 'guven_fatura_analiz'},
            USER_NAME=creds['username'],
            PASSWORD=creds['password'],
        )
        session_id = (
            login_resp.SESSION_ID if hasattr(login_resp, 'SESSION_ID') else str(login_resp)
        )

        request_header = {
            'SESSION_ID': session_id,
            'APPLICATION_NAME': 'guven_fatura_analiz',
            'COMPRESSED': 'N',
        }

        return client, session_id, request_header

    @api.model
    def _get_earsiv_soap_client(self, company=None):
        """E-Arşiv SOAP client oluştur (login e-fatura WSDL üzerinden).

        Returns:
            tuple: (efatura_client, earsiv_client, session_id, request_header)
        """
        company = company or self.env.company
        efatura_client, session_id, request_header = self._get_soap_client_and_login(company)

        from zeep import Client
        from zeep.transports import Transport
        from zeep import Settings

        creds = company.get_efatura_credentials()
        earsiv_ws = creds.get('earsiv_ws_url') or \
            'https://earsivws.izibiz.com.tr/EIArchiveWS/EFaturaArchive?wsdl'

        session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(pool_connections=5, pool_maxsize=5, max_retries=retry)
        session.mount('https://', adapter)

        transport = Transport(session=session, timeout=90)
        settings = Settings(strict=False, xml_huge_tree=True)
        earsiv_client = Client(earsiv_ws, transport=transport, settings=settings)

        return efatura_client, earsiv_client, session_id, request_header

    # ==================================================================
    # PARSE HELPERS
    # ==================================================================

    @api.model
    def _parse_date_field(self, date_string):
        """Çeşitli tarih formatlarını parse et → fields.Date uyumlu string veya None."""
        if not date_string:
            return None
        date_string = str(date_string).strip()

        # Timezone offset'li sadece tarih: 2025-01-01+03:00
        if '+' in date_string and 'T' not in date_string:
            date_part = date_string.split('+')[0]
            if len(date_part) == 10:
                return date_part  # YYYY-MM-DD

        # ISO format: 2025-01-01T14:30:00+03:00
        if 'T' in date_string:
            return date_string[:10]  # Sadece tarih kısmı

        # YYYY-MM-DD
        if len(date_string) == 10 and '-' in date_string:
            return date_string

        # Fallback formatlar
        for fmt in ('%d.%m.%Y', '%d/%m/%Y', '%d-%m-%Y', '%Y%m%d',
                    '%d-%m-%Y %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(date_string, fmt).strftime('%Y-%m-%d')
            except ValueError:
                continue

        _logger.warning("[GUVEN-PARSE] Tarih parse edilemedi: %s", date_string)
        return None

    @staticmethod
    def _parse_float(value):
        """Finansal string → float. Türkçe format desteği (1.234,56)."""
        if not value:
            return 0.0
        s = str(value).strip()
        if ',' in s and '.' in s:
            s = s.replace('.', '').replace(',', '.')
        elif ',' in s:
            s = s.replace(',', '.')
        s = s.replace('₺', '').replace('TL', '').replace('$', '').strip()
        try:
            return float(s)
        except (ValueError, TypeError):
            return 0.0

    # ==================================================================
    # HEADER COMPARE & UPDATE
    # ==================================================================

    def _compare_and_update_header(self, existing, vals):
        """Compare SOAP vals with existing record. Write only changed fields.

        If any header field changed, also resets details_received so
        the cron re-fetches and re-parses the full XML.
        Caller must check is_locked before calling this method.
        Returns True if the record was updated, False if nothing changed.
        """
        from datetime import date as date_type

        skip = {'company_id', 'kaynak', 'direction', 'uuid'}
        changed = {}

        for key, new_val in vals.items():
            if key in skip:
                continue
            old_val = existing[key]
            # Many2one → compare as int
            if isinstance(old_val, models.BaseModel):
                old_val = old_val.id if old_val else False
            # Normalize None → False
            if new_val is None:
                new_val = False
            if old_val is None:
                old_val = False
            # Normalize empty strings → False
            if isinstance(old_val, str) and not old_val:
                old_val = False
            if isinstance(new_val, str) and not new_val:
                new_val = False
            # Date comparison: DB stores date objects, SOAP may return strings
            if isinstance(old_val, date_type) and isinstance(new_val, str):
                try:
                    new_val = date_type.fromisoformat(new_val)
                except (ValueError, TypeError):
                    pass
            elif isinstance(new_val, date_type) and isinstance(old_val, str):
                try:
                    old_val = date_type.fromisoformat(old_val)
                except (ValueError, TypeError):
                    pass
            # Float comparison with tolerance
            if isinstance(new_val, float):
                old_float = float(old_val) if old_val else 0.0
                if abs(old_float - new_val) > 0.005:
                    changed[key] = new_val
                continue
            # Generic comparison (strings, dates, booleans, integers)
            if old_val != new_val:
                changed[key] = new_val

        if not changed:
            return False

        _logger.info(
            "[GUVEN-SYNC] %s değişen alanlar: %s",
            existing.invoice_id,
            {k: (existing[k], v) for k, v in changed.items()},
        )

        # Header changed → reset details_received so XML gets re-parsed
        if existing.details_received:
            changed['details_received'] = False
        existing.write(changed)
        return True

    # ==================================================================
    # HEADER SYNC (SOAP HEADER_ONLY=Y)
    # ==================================================================

    @api.model
    def _sync_efatura_headers(self, start_date, end_date, direction, company=None):
        """HEADER_ONLY=Y ile e-fatura header'larını çek ve DB'ye kaydet."""
        company = company or self.env.company
        client, session_id, request_header = self._get_soap_client_and_login(company)

        try:
            search_key = {
                'LIMIT': 25000,
                'START_DATE': datetime.combine(start_date, datetime.min.time()),
                'END_DATE': datetime.combine(end_date, datetime.max.time()),
                'READ_INCLUDED': 'true',
                'DIRECTION': direction,
            }

            with client.settings(raw_response=True):
                raw = client.service.GetInvoice(
                    REQUEST_HEADER=request_header,
                    INVOICE_SEARCH_KEY=search_key,
                    HEADER_ONLY='Y',
                )

            root = ET.fromstring(raw.content)

            # INVOICE elementlerini bul (namespace-agnostic)
            invoice_elems = root.findall('.//INVOICE')
            if not invoice_elems:
                invoice_elems = [e for e in root.iter() if e.tag.endswith('INVOICE')]

            created = updated = 0
            for inv_elem in invoice_elems:
                header = inv_elem.find('HEADER')
                if header is None:
                    header = next(
                        (e for e in inv_elem.iter() if e.tag.endswith('HEADER')), None
                    )

                h = {}
                if header is not None:
                    for child in header:
                        tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                        h[tag] = child.text

                uuid = inv_elem.get('UUID') or h.get('UUID')
                if not uuid:
                    continue

                raw_profile = h.get('PROFILEID')
                raw_type = h.get('INVOICE_TYPE_CODE')
                if raw_profile and raw_profile not in _VALID_PROFILE_IDS:
                    _logger.warning(
                        "[GUVEN-EFATURA] Bilinmeyen profile_id: %r (fatura: %s)", raw_profile, uuid,
                    )
                if raw_type and raw_type not in _VALID_INVOICE_TYPE_CODES:
                    _logger.warning(
                        "[GUVEN-EFATURA] Bilinmeyen invoice_type_code: %r (fatura: %s)", raw_type, uuid,
                    )

                vals = {
                    'invoice_id': inv_elem.get('ID') or h.get('ID', ''),
                    'uuid': uuid,
                    'sender': h.get('SENDER'),
                    'sender_name': h.get('SUPPLIER'),
                    'receiver': h.get('RECEIVER'),
                    'receiver_name': h.get('CUSTOMER'),
                    'profile_id': raw_profile if raw_profile in _VALID_PROFILE_IDS else False,
                    'invoice_type_code': raw_type if raw_type in _VALID_INVOICE_TYPE_CODES else False,
                    'status_code': h.get('STATUS_CODE') or h.get('STATUS'),
                    'status_description': h.get('STATUS_DESCRIPTION'),
                    'response_code': h.get('RESPONSE_CODE'),
                    'direction': direction,
                    'kaynak': 'e-fatura-izibiz',
                    'company_id': company.id,
                }

                # SOAP'tan currency geliyorsa ekle (HEADER_ONLY boş dönebilir)
                soap_currency = h.get('CURRENCY_CODE')
                if soap_currency:
                    vals['currency_code'] = soap_currency

                # Tarih
                if h.get('ISSUE_DATE'):
                    vals['issue_date'] = self._parse_date_field(h['ISSUE_DATE'])

                # Finansal alanlar
                for soap_f, odoo_f in (
                    ('PAYABLE_AMOUNT', 'payable_amount'),
                    ('TAX_EXCLUSIVE_TOTAL_AMOUNT', 'tax_exclusive_amount'),
                    ('TAX_INCLUSIVE_TOTAL_AMOUNT', 'tax_inclusive_amount'),
                    ('ALLOWANCE_TOTAL_AMOUNT', 'allowance_total_amount'),
                ):
                    if h.get(soap_f):
                        vals[odoo_f] = self._parse_float(h[soap_f])

                # Upsert
                existing = self.search([
                    ('uuid', '=', uuid),
                    ('kaynak', '=', 'e-fatura-izibiz'),
                    ('company_id', '=', company.id),
                ], limit=1)
                if existing:
                    if existing.is_locked:
                        continue
                    if self._compare_and_update_header(existing, vals):
                        updated += 1
                else:
                    vals['details_received'] = False
                    self.create(vals)
                    created += 1

            return {'created': created, 'updated': updated}

        finally:
            try:
                client.service.Logout(REQUEST_HEADER=request_header)
            except Exception:
                pass

    # ==================================================================
    # E-ARŞİV HEADER SYNC
    # ==================================================================

    @api.model
    def _link_cancellation_to_original(self, cancel_record, invoice_id, company):
        """İptal kaydını aynı invoice_id'li asıl faturaya bağla (7 gün kuralı)."""
        from datetime import timedelta

        search_domain = [
            ('invoice_id', '=', invoice_id),
            ('kaynak', '=', 'e-arsiv-izibiz'),
            ('is_cancellation', '=', False),
            ('company_id', '=', company.id),
        ]

        if cancel_record.issue_date:
            min_date = cancel_record.issue_date - timedelta(days=7)
            search_domain.extend([
                ('issue_date', '>=', min_date),
                ('issue_date', '<=', cancel_record.issue_date),
            ])

        original = self.search(search_domain, limit=1, order='issue_date desc')
        if original:
            cancel_record.write({'cancelled_invoice_id': original.id})
            _logger.info("[GUVEN-EARSIV] E-Arşiv iptal ilişkisi: %s -> ID:%d", invoice_id, original.id)
        else:
            _logger.debug("[GUVEN-EARSIV] E-Arşiv iptal: asıl fatura bulunamadı: %s", invoice_id)

    @api.model
    def _sync_earsiv_headers(self, start_date, end_date, company=None):
        """HEADER_ONLY=Y ile e-arşiv header'larını çek ve DB'ye kaydet."""
        company = company or self.env.company
        efatura_client, earsiv_client, session_id, request_header = \
            self._get_earsiv_soap_client(company)

        try:
            with earsiv_client.settings(raw_response=True):
                raw = earsiv_client.service.GetEArchiveInvoiceList(
                    REQUEST_HEADER=request_header,
                    LIMIT=25000,
                    START_DATE=datetime.combine(start_date, datetime.min.time()),
                    END_DATE=datetime.combine(end_date, datetime.max.time()),
                    HEADER_ONLY='Y',
                    READ_INCLUDED='true',
                )

            root = ET.fromstring(raw.content)

            # INVOICE elementlerini bul (namespace-agnostic)
            invoice_elems = root.findall('.//INVOICE')
            if not invoice_elems:
                invoice_elems = [e for e in root.iter() if e.tag.endswith('INVOICE')]

            created = updated = 0
            cancellation_list = []

            for inv_elem in invoice_elems:
                header = inv_elem.find('HEADER')
                if header is None:
                    header = next(
                        (e for e in inv_elem.iter() if e.tag.endswith('HEADER')), None
                    )

                h = {}
                if header is not None:
                    for child in header:
                        tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                        h[tag] = child.text

                uuid = inv_elem.get('UUID') or h.get('UUID')
                if not uuid:
                    continue

                raw_profile = h.get('PROFILE_ID') or h.get('PROFILEID') or h.get('PROFILE')
                raw_type = h.get('INVOICE_TYPE') or h.get('INVOICE_TYPE_CODE')

                validated_profile = raw_profile if raw_profile in _VALID_PROFILE_IDS else False
                validated_type = raw_type if raw_type in _VALID_INVOICE_TYPE_CODES else False

                if raw_profile and raw_profile not in _VALID_PROFILE_IDS:
                    _logger.warning(
                        "[GUVEN-EARSIV] Bilinmeyen profile_id: %r (fatura: %s)", raw_profile, uuid,
                    )
                if raw_type and raw_type not in _VALID_INVOICE_TYPE_CODES:
                    _logger.warning(
                        "[GUVEN-EARSIV] Bilinmeyen invoice_type_code: %r (fatura: %s)", raw_type, uuid,
                    )

                # İptal kayıtlarını ayrı listede topla (normal kayıtlardan sonra işlenecek)
                if validated_profile == 'IPTAL':
                    cancellation_list.append((inv_elem, h, uuid))
                    continue

                vals = {
                    'invoice_id': inv_elem.get('ID') or h.get('INVOICE_ID', ''),
                    'uuid': uuid,
                    'sender': h.get('SENDER_IDENTIFIER'),
                    'sender_name': h.get('SENDER_NAME'),
                    'receiver': h.get('CUSTOMER_IDENTIFIER'),
                    'receiver_name': h.get('CUSTOMER_NAME'),
                    'profile_id': validated_profile,
                    'invoice_type_code': validated_type,
                    'status_code': h.get('STATUS_CODE') or h.get('STATUS'),
                    'direction': 'OUT',
                    'kaynak': 'e-arsiv-izibiz',
                    'company_id': company.id,
                    'is_cancellation': False,
                }

                # SOAP'tan currency geliyorsa ekle (HEADER_ONLY boş dönebilir)
                soap_currency = h.get('CURRENCY_CODE')
                if soap_currency:
                    vals['currency_code'] = soap_currency

                # Tarih
                if h.get('ISSUE_DATE'):
                    vals['issue_date'] = self._parse_date_field(h['ISSUE_DATE'])

                # Finansal alanlar
                # NOT: E-arşiv SOAP TAXABLE_AMOUNT aslında vergi tutarını döner,
                # matrah (tax_exclusive_amount) değil. Doğru matrah UBL XML parse'tan gelir.
                if h.get('PAYABLE_AMOUNT'):
                    vals['payable_amount'] = self._parse_float(h['PAYABLE_AMOUNT'])

                # Upsert — iptal edilmiş kayıtları normal flow'da güncelleme
                existing = self.search([
                    ('uuid', '=', uuid),
                    ('kaynak', '=', 'e-arsiv-izibiz'),
                    ('company_id', '=', company.id),
                ], limit=1)
                if existing:
                    if existing.is_locked:
                        continue
                    # İptal edilmiş kayıtları normal flow'da güncelleme (ping-pong önleme)
                    # Aynı UUID hem EARSIVFATURA hem IPTAL olarak gelir; iptal flow authoritative
                    if existing.is_cancellation:
                        continue
                    if self._compare_and_update_header(existing, vals):
                        updated += 1
                else:
                    vals['details_received'] = False
                    self.create(vals)
                    created += 1

            # --- İptal kayıtlarını işle (normal kayıtlardan sonra) ---
            for inv_elem, h, uuid in cancellation_list:
                cancel_invoice_id = inv_elem.get('ID') or h.get('INVOICE_ID', '')

                raw_type = h.get('INVOICE_TYPE') or h.get('INVOICE_TYPE_CODE')
                validated_type = raw_type if raw_type in _VALID_INVOICE_TYPE_CODES else False

                cancel_vals = {
                    'invoice_id': cancel_invoice_id,
                    'uuid': uuid,
                    'sender': h.get('SENDER_IDENTIFIER'),
                    'sender_name': h.get('SENDER_NAME'),
                    'receiver': h.get('CUSTOMER_IDENTIFIER'),
                    'receiver_name': h.get('CUSTOMER_NAME'),
                    'profile_id': 'IPTAL',
                    'invoice_type_code': validated_type,
                    'status_code': h.get('STATUS_CODE') or h.get('STATUS'),
                    'direction': 'OUT',
                    'kaynak': 'e-arsiv-izibiz',
                    'company_id': company.id,
                    'is_cancellation': True,
                }

                # SOAP'tan currency geliyorsa ekle (HEADER_ONLY boş dönebilir)
                soap_currency = h.get('CURRENCY_CODE')
                if soap_currency:
                    cancel_vals['currency_code'] = soap_currency

                if h.get('ISSUE_DATE'):
                    cancel_vals['issue_date'] = self._parse_date_field(h['ISSUE_DATE'])

                # NOT: E-arşiv SOAP TAXABLE_AMOUNT = vergi tutarı, matrah değil
                if h.get('PAYABLE_AMOUNT'):
                    cancel_vals['payable_amount'] = self._parse_float(h['PAYABLE_AMOUNT'])

                existing_cancel = self.search([
                    ('uuid', '=', uuid),
                    ('kaynak', '=', 'e-arsiv-izibiz'),
                    ('company_id', '=', company.id),
                ], limit=1)

                if existing_cancel:
                    if existing_cancel.is_locked:
                        continue
                    if self._compare_and_update_header(existing_cancel, cancel_vals):
                        updated += 1
                    if not existing_cancel.cancelled_invoice_id:
                        self._link_cancellation_to_original(
                            existing_cancel, cancel_invoice_id, company,
                        )
                else:
                    cancel_vals['details_received'] = False
                    new_cancel = self.create(cancel_vals)
                    self._link_cancellation_to_original(
                        new_cancel, cancel_invoice_id, company,
                    )
                    created += 1

            return {'created': created, 'updated': updated}

        finally:
            try:
                efatura_client.service.Logout(REQUEST_HEADER=request_header)
            except Exception:
                pass

    # ==================================================================
    # UBL XML PARSE
    # ==================================================================

    def _parse_ubl_and_update(self, ubl_xml_bytes):
        """UBL XML'i parse et, line/tax/note kayıtlarını oluştur, eksik alanları güncelle."""
        self.ensure_one()

        ns = {
            'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
            'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
        }

        root = ET.fromstring(ubl_xml_bytes)

        def get_text(elem, xpath):
            """Namespace-aware XPath, fallback to local name."""
            if elem is None:
                return ''
            found = elem.find(xpath, ns)
            if found is not None and found.text:
                return found.text.strip()
            # Fallback: local name search
            local = xpath.split(':')[-1] if ':' in xpath else xpath.lstrip('./')
            for child in elem.iter():
                tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                if tag == local and child.text:
                    return child.text.strip()
            return ''

        def find_elem(parent, local_name):
            if parent is None:
                return None
            for child in parent.iter():
                tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                if tag == local_name:
                    return child
            return None

        def find_all_elems(parent, local_name):
            result = []
            if parent is None:
                return result
            for child in parent:
                tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                if tag == local_name:
                    result.append(child)
            return result

        # TaxScheme ID → tax_type mapping
        SCHEME_ID_MAP = {
            '0015': 'kdv',
            '0003': 'kdv',       # Bazı implementasyonlarda kullanılır
            '9015': 'withholding',
            '4071': 'bsmv',
            '0059': 'konaklama',
            '0071': 'tuketim',
        }
        # TaxScheme Name fallback → tax_type
        SCHEME_NAME_MAP = {
            # KDV varyasyonları
            'KDV': 'kdv',
            'KDV HESAPLANAN': 'kdv',
            'KATMA DEGER VERGISI': 'kdv',
            'KATMA DEĞER VERGISI': 'kdv',
            'KATMA DEĞER VERGİSİ': 'kdv',
            'GERÇEK USULDE KATMA DEĞER VERGİSİ': 'kdv',
            'KDV-SATIŞLAR': 'kdv',
            'KDV GERÇEK': 'kdv',
            'KDV GERCEK': 'kdv',
            'KDV VERGISI': 'kdv',
            'KDV VERGİSİ': 'kdv',
            'HESAPLANAN KDV': 'kdv',
            'SATIŞ VERGISI': 'kdv',
            'SATIŞ VERGİSİ': 'kdv',
            'SATIŞ KDV': 'kdv',
            # Tevkifat
            'TEVKIFAT': 'withholding',
            'KDV TEVKİFAT': 'withholding',
            # BSMV
            'BSMV': 'bsmv',
            # Konaklama
            'KONAKLAMA VERGISI': 'konaklama',
            'KONAKLAMA VERGİSİ': 'konaklama',
            # Tüketim
            'ELK.HAVAGAZ.TÜK.VER.': 'tuketim',
            'ELEKTRIK HAVAGAZI TUKETIM VERGISI': 'tuketim',
            # Özel İletişim Vergisi (ÖİV)
            'ÖZEL ILETISIM VERGISI': 'oiv',
            'ÖZEL İLETİŞİM VERGİSİ': 'oiv',
            'ÖİV': 'oiv',
            'OIV': 'oiv',
            'ÖZEL İLETIŞIM VERGISI': 'oiv',
            'Ö.ILETISIM V': 'oiv',
            # Damga Vergisi
            'DAMGA VERGISI': 'damga',
            'DAMGA VERGİSİ': 'damga',
        }

        def resolve_tax_type(subtotal_elem):
            """TaxSubtotal → tax_type çözümleme (scheme ID + name fallback + fuzzy)."""
            # TaxScheme elementini bul
            scheme = find_elem(subtotal_elem, 'TaxScheme')
            if scheme is None:
                return False

            # Scheme ID: doğrudan children arasında 'ID' ara
            scheme_id = ''
            scheme_name = ''
            for child in scheme:
                child_tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                if child_tag == 'ID' and child.text:
                    scheme_id = child.text.strip()
                elif child_tag == 'Name' and child.text:
                    scheme_name = child.text.strip().upper()

            # 1. Scheme ID ile exact match
            if scheme_id in SCHEME_ID_MAP:
                return SCHEME_ID_MAP[scheme_id]
            # 2. Scheme Name ile exact match
            if scheme_name in SCHEME_NAME_MAP:
                return SCHEME_NAME_MAP[scheme_name]
            # 3. Parantez içeriğini kaldırıp tekrar dene
            cleaned = re.sub(r'\s*\(.*?\)', '', scheme_name).strip()
            if cleaned and cleaned in SCHEME_NAME_MAP:
                return SCHEME_NAME_MAP[cleaned]
            # 4. Oran bilgisini kaldırıp tekrar dene ("%20" gibi)
            cleaned2 = re.sub(r'\s*%\d+', '', cleaned).strip()
            if cleaned2 and cleaned2 in SCHEME_NAME_MAP:
                return SCHEME_NAME_MAP[cleaned2]
            # 5. İçinde KDV/KATMA geçiyorsa kdv kabul et
            if 'KDV' in scheme_name or 'KATMA' in scheme_name:
                return 'kdv'

            if scheme_id or scheme_name:
                _logger.warning(
                    "[GUVEN-PARSE] Bilinmeyen TaxScheme ID=%r Name=%r (fatura: %s)",
                    scheme_id, scheme_name, self.invoice_id,
                )
            return 'diger'

        pf = self._parse_float
        vals = {}

        # --- Issue Time ---
        issue_time = get_text(root, './/cbc:IssueTime')
        if issue_time:
            vals['issue_time'] = issue_time[:8]  # HH:MM:SS

        # --- Currency ---
        currency = get_text(root, './/cbc:DocumentCurrencyCode')
        if currency:
            vals['currency_code'] = currency

        # --- Exchange Rate (PricingExchangeRate) ---
        pricing_er = find_elem(root, 'PricingExchangeRate')
        exchange_rate = 0.0
        if pricing_er is not None:
            rate_text = get_text(pricing_er, './/cbc:CalculationRate')
            if rate_text:
                exchange_rate = pf(rate_text)
                if exchange_rate > 0:
                    vals['exchange_rate'] = exchange_rate

        # --- Toplam Tutarlar ---
        legal_total = find_elem(root, 'LegalMonetaryTotal')
        if legal_total is not None:
            for xml_f, odoo_f in (
                ('TaxExclusiveAmount', 'tax_exclusive_amount'),
                ('TaxInclusiveAmount', 'tax_inclusive_amount'),
                ('PayableAmount', 'payable_amount'),
                ('AllowanceTotalAmount', 'allowance_total_amount'),
            ):
                v = get_text(legal_total, f'.//cbc:{xml_f}')
                if v:
                    vals[odoo_f] = pf(v)

        # --- TRY Hesaplamaları ---
        cur = currency or self.currency_code or 'TRY'
        rate = exchange_rate or self.exchange_rate or 1.0
        for base_f, try_f in (
            ('tax_exclusive_amount', 'tax_exclusive_amount_try'),
            ('tax_inclusive_amount', 'tax_inclusive_amount_try'),
            ('payable_amount', 'payable_amount_try'),
            ('allowance_total_amount', 'allowance_total_amount_try'),
        ):
            amount = vals.get(base_f) or getattr(self, base_f, 0.0)
            if cur == 'TRY':
                vals[try_f] = amount
            elif rate > 0:
                vals[try_f] = amount * rate

        # --- Mevcut child'ları temizle (yeniden oluşturmak için) ---
        self.tax_ids.unlink()
        self.line_ids.unlink()
        self.note_ids.unlink()

        # --- Notlar ---
        note_lines = find_all_elems(root, 'Note')
        note_vals_list = []
        for seq, note_elem in enumerate(note_lines, 1):
            text = note_elem.text.strip() if note_elem.text else ''
            if text:
                note_vals_list.append({
                    'fatura_id': self.id,
                    'note_type': 'hashtag' if text.startswith('#') else 'free_text',
                    'value': text,
                    'sequence': seq * 10,
                })
        if note_vals_list:
            self.env['guven.fatura.note'].create(note_vals_list)

        # --- Fatura Kalemleri ---
        invoice_lines = find_all_elems(root, 'InvoiceLine')
        for line_no, line_elem in enumerate(invoice_lines, 1):
            item = find_elem(line_elem, 'Item')
            item_name = get_text(item, './/cbc:Name') if item is not None else ''
            if not item_name and item is not None:
                item_name = get_text(item, './/cbc:Description')

            qty_elem = find_elem(line_elem, 'InvoicedQuantity')
            quantity = pf(qty_elem.text) if qty_elem is not None and qty_elem.text else 1.0

            line_ext = pf(get_text(line_elem, './/cbc:LineExtensionAmount'))

            # Satır indirimi (AllowanceCharge)
            allowance = 0.0
            ac = find_elem(line_elem, 'AllowanceCharge')
            if ac is not None:
                allowance = pf(get_text(ac, './/cbc:Amount'))

            # TRY hesaplama
            line_ext_try = line_ext if cur == 'TRY' else line_ext * rate if rate > 0 else 0.0
            allow_try = allowance if cur == 'TRY' else allowance * rate if rate > 0 else 0.0

            line_record = self.env['guven.fatura.line'].create({
                'fatura_id': self.id,
                'line_no': line_no,
                'item_name': item_name,
                'quantity': quantity,
                'line_extension_amount': line_ext,
                'allowance_amount': allowance,
                'currency_code': cur,
                'line_extension_amount_try': line_ext_try,
                'allowance_amount_try': allow_try,
            })

            # --- Satır Seviyesi Vergiler ---
            tax_total = find_elem(line_elem, 'TaxTotal')
            if tax_total is not None:
                for subtotal in find_all_elems(tax_total, 'TaxSubtotal'):
                    taxable = pf(get_text(subtotal, './/cbc:TaxableAmount'))
                    tax_amt = pf(get_text(subtotal, './/cbc:TaxAmount'))
                    percent = pf(get_text(subtotal, './/cbc:Percent'))
                    tax_type = resolve_tax_type(subtotal)

                    taxable_try = (
                        taxable if cur == 'TRY'
                        else taxable * rate if rate > 0 else 0.0
                    )
                    tax_amt_try = (
                        tax_amt if cur == 'TRY'
                        else tax_amt * rate if rate > 0 else 0.0
                    )

                    self.env['guven.fatura.tax'].create({
                        'fatura_id': self.id,
                        'line_id': line_record.id,
                        'tax_type': tax_type,
                        'taxable_amount': taxable,
                        'tax_amount': tax_amt,
                        'percent': percent,
                        'currency_code': cur,
                        'taxable_amount_try': taxable_try,
                        'tax_amount_try': tax_amt_try,
                    })

            # --- Satır Seviyesi Tevkifat (WithholdingTaxTotal) ---
            wh_tax_total = find_elem(line_elem, 'WithholdingTaxTotal')
            if wh_tax_total is not None:
                for subtotal in find_all_elems(wh_tax_total, 'TaxSubtotal'):
                    taxable = pf(get_text(subtotal, './/cbc:TaxableAmount'))
                    tax_amt = pf(get_text(subtotal, './/cbc:TaxAmount'))
                    percent = pf(get_text(subtotal, './/cbc:Percent'))

                    taxable_try = (
                        taxable if cur == 'TRY'
                        else taxable * rate if rate > 0 else 0.0
                    )
                    tax_amt_try = (
                        tax_amt if cur == 'TRY'
                        else tax_amt * rate if rate > 0 else 0.0
                    )

                    self.env['guven.fatura.tax'].create({
                        'fatura_id': self.id,
                        'line_id': line_record.id,
                        'tax_type': 'withholding',
                        'taxable_amount': taxable,
                        'tax_amount': tax_amt,
                        'percent': percent,
                        'currency_code': cur,
                        'taxable_amount_try': taxable_try,
                        'tax_amount_try': tax_amt_try,
                    })

        # --- Fatura Seviyesi Vergiler (root TaxTotal) ---
        # Satır-level vergiler zaten oluşturulduysa root-level atla (aynı veriyi tekrarlar)
        has_line_taxes = self.env['guven.fatura.tax'].search_count([
            ('fatura_id', '=', self.id), ('line_id', '!=', False),
        ])
        if not has_line_taxes:
            for tax_total in find_all_elems(root, 'TaxTotal'):
                for subtotal in find_all_elems(tax_total, 'TaxSubtotal'):
                    taxable = pf(get_text(subtotal, './/cbc:TaxableAmount'))
                    tax_amt = pf(get_text(subtotal, './/cbc:TaxAmount'))
                    percent = pf(get_text(subtotal, './/cbc:Percent'))
                    tax_type = resolve_tax_type(subtotal)

                    taxable_try = (
                        taxable if cur == 'TRY'
                        else taxable * rate if rate > 0 else 0.0
                    )
                    tax_amt_try = (
                        tax_amt if cur == 'TRY'
                        else tax_amt * rate if rate > 0 else 0.0
                    )

                    self.env['guven.fatura.tax'].create({
                        'fatura_id': self.id,
                        'line_id': False,
                        'tax_type': tax_type,
                        'taxable_amount': taxable,
                        'tax_amount': tax_amt,
                        'percent': percent,
                        'currency_code': cur,
                        'taxable_amount_try': taxable_try,
                        'tax_amount_try': tax_amt_try,
                    })

        # --- Fatura Seviyesi Tevkifat (root WithholdingTaxTotal) ---
        has_line_wh = self.env['guven.fatura.tax'].search_count([
            ('fatura_id', '=', self.id),
            ('line_id', '!=', False),
            ('tax_type', '=', 'withholding'),
        ])
        if not has_line_wh:
            for wh_total in find_all_elems(root, 'WithholdingTaxTotal'):
                for subtotal in find_all_elems(wh_total, 'TaxSubtotal'):
                    taxable = pf(get_text(subtotal, './/cbc:TaxableAmount'))
                    tax_amt = pf(get_text(subtotal, './/cbc:TaxAmount'))
                    percent = pf(get_text(subtotal, './/cbc:Percent'))

                    taxable_try = (
                        taxable if cur == 'TRY'
                        else taxable * rate if rate > 0 else 0.0
                    )
                    tax_amt_try = (
                        tax_amt if cur == 'TRY'
                        else tax_amt * rate if rate > 0 else 0.0
                    )

                    self.env['guven.fatura.tax'].create({
                        'fatura_id': self.id,
                        'line_id': False,
                        'tax_type': 'withholding',
                        'taxable_amount': taxable,
                        'tax_amount': tax_amt,
                        'percent': percent,
                        'currency_code': cur,
                        'taxable_amount_try': taxable_try,
                        'tax_amount_try': tax_amt_try,
                    })

        self.write(vals)

    # ==================================================================
    # XML FETCH + PARSE (tek fatura)
    # ==================================================================

    def _fetch_and_parse_xml(self, client, request_header):
        """Tek fatura için HEADER_ONLY=N ile XML çek, parse et."""
        self.ensure_one()

        search_key = {
            'LIMIT': 1,
            'UUID': self.uuid,
            'DIRECTION': self.direction or 'IN',
            'READ_INCLUDED': 'true',
        }

        with client.settings(raw_response=True):
            raw = client.service.GetInvoice(
                REQUEST_HEADER=request_header,
                INVOICE_SEARCH_KEY=search_key,
                HEADER_ONLY='N',
            )

        root = ET.fromstring(raw.content)

        # CONTENT elementini bul (en uzun text'li element)
        content_text = None
        max_len = 0
        for tag in ('CONTENT', 'INVOICE', 'HTML_CONTENT', 'INVOICE_CONTENT', 'DATA'):
            for elem in root.iter():
                t = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                if t == tag and elem.text and len(elem.text.strip()) > max_len:
                    content_text = elem.text.strip()
                    max_len = len(content_text)

        if not content_text or max_len < 100:
            raise UserError(_("Fatura %s için XML içeriği alınamadı.") % self.invoice_id)

        # Base64 decode
        decoded = base64.b64decode(content_text)

        # ZIP kontrolü
        if decoded[:4] == b'PK\x03\x04':
            with zipfile.ZipFile(io.BytesIO(decoded), 'r') as zf:
                xml_name = next(
                    (n for n in zf.namelist()
                     if n.endswith('.xml') and not n.startswith('__')),
                    zf.namelist()[0] if zf.namelist() else None,
                )
                if not xml_name:
                    raise UserError(
                        _("Fatura %s ZIP içinde XML bulunamadı.") % self.invoice_id
                    )
                ubl_bytes = zf.read(xml_name)
        else:
            ubl_bytes = decoded

        # NUL karakterlerini temizle
        ubl_bytes = ubl_bytes.replace(b'\x00', b'')

        # Parse et ve güncelle
        self._parse_ubl_and_update(ubl_bytes)
        self.write({'details_received': True})

    def _fetch_earsiv_xml(self, earsiv_client, request_header):
        """Tek e-arşiv faturası için ReadFromArchive ile XML çek, parse et."""
        self.ensure_one()

        with earsiv_client.settings(raw_response=True):
            raw = earsiv_client.service.ReadFromArchive(
                REQUEST_HEADER=request_header,
                INVOICEID=self.uuid,
                PORTAL_DIRECTION='OUT',
                PROFILE='XML',
            )

        root = ET.fromstring(raw.content)

        # SOAP hata kontrolü
        error_code = None
        for elem in root.iter():
            t = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
            if t == 'ERROR_CODE' and elem.text:
                error_code = elem.text.strip()
            elif t == 'ERROR_SHORT_DES' and elem.text and error_code and error_code != '0':
                raise UserError(
                    _("E-Arşiv fatura %s SOAP hatası: [%s] %s")
                    % (self.invoice_id, error_code, elem.text.strip())
                )

        # CONTENT elementini bul (en uzun text'li element)
        content_text = None
        max_len = 0
        for tag in ('CONTENT', 'INVOICE', 'HTML_CONTENT', 'INVOICE_CONTENT', 'DATA'):
            for elem in root.iter():
                t = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
                if t == tag and elem.text and len(elem.text.strip()) > max_len:
                    content_text = elem.text.strip()
                    max_len = len(content_text)

        if not content_text or max_len < 100:
            raise UserError(_("E-Arşiv fatura %s için XML içeriği alınamadı.") % self.invoice_id)

        # Base64 decode
        decoded = base64.b64decode(content_text)

        # ZIP kontrolü
        if decoded[:4] == b'PK\x03\x04':
            with zipfile.ZipFile(io.BytesIO(decoded), 'r') as zf:
                xml_name = next(
                    (n for n in zf.namelist()
                     if n.endswith('.xml') and not n.startswith('__')),
                    zf.namelist()[0] if zf.namelist() else None,
                )
                if not xml_name:
                    raise UserError(
                        _("E-Arşiv fatura %s ZIP içinde XML bulunamadı.") % self.invoice_id
                    )
                ubl_bytes = zf.read(xml_name)
        else:
            ubl_bytes = decoded

        # NUL karakterlerini temizle
        ubl_bytes = ubl_bytes.replace(b'\x00', b'')

        # Parse et ve güncelle
        self._parse_ubl_and_update(ubl_bytes)
        self.write({'details_received': True})

    # ==================================================================
    # CRON: XML DETAY ÇEKME
    # ==================================================================

    # PostgreSQL advisory lock ID'leri (çakışma önleme).
    # Rastgele büyük sabitler — başka modüllerle çakışmaması için.
    _LOCK_EFATURA_DETAIL = 737001
    _LOCK_EARSIV_DETAIL = 737002
    _LOCK_HEADER_SYNC = 737003

    @api.model
    def _try_advisory_lock(self, lock_id):
        """PostgreSQL session-level advisory lock almayı dene.

        Returns True if lock acquired, False if another session holds it.
        Lock, DB session kapandığında otomatik serbest kalır.
        """
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
        return self.env.cr.fetchone()[0]

    @api.model
    def _release_advisory_lock(self, lock_id):
        """PostgreSQL session-level advisory lock'u serbest bırak."""
        self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))

    @api.model
    def _cron_fetch_invoice_details(self):
        """details_received=False e-fatura kayıtların XML detayını çek ve parse et."""
        if not self._try_advisory_lock(self._LOCK_EFATURA_DETAIL):
            _logger.info("[GUVEN-EFATURA] Önceki çalışma devam ediyor, atlanıyor.")
            return

        _logger.info("[GUVEN-EFATURA] Cron başladı.")
        t0 = time.time()
        try:
            self._do_fetch_invoice_details()
        finally:
            elapsed = time.time() - t0
            remaining = self.sudo()._read_group(
                [('details_received', '=', False), ('kaynak', '=', 'e-fatura-izibiz')],
                groupby=['company_id'],
                aggregates=['__count'],
            )
            for company, count in remaining:
                _logger.info(
                    "[GUVEN-EFATURA] Kalan: %d fatura [%s]",
                    count, company.name if company else 'Şirketsiz',
                )
            if not remaining:
                _logger.info("[GUVEN-EFATURA] Kalan: 0 fatura (tümü tamamlandı)")
            _logger.info("[GUVEN-EFATURA] Cron bitti. Süre: %.1f sn", elapsed)
            self._release_advisory_lock(self._LOCK_EFATURA_DETAIL)

    @api.model
    def _do_fetch_invoice_details(self):
        """E-fatura XML detay çekme iç implementasyonu."""
        BATCH_SIZE = 100
        COMMIT_EVERY = 50

        # Bekleyen faturası olan şirketleri bul
        company_groups = self.sudo()._read_group(
            [('details_received', '=', False), ('kaynak', '=', 'e-fatura-izibiz')],
            groupby=['company_id'],
            aggregates=['__count'],
        )
        company_ids = [company.id for company, _count in company_groups if company]
        if not company_ids:
            return

        companies = self.env['res.company'].sudo().browse(company_ids)

        for company in companies:
            # Her şirket için ayrı 100'er fatura çek
            inv_set = self.search([
                ('details_received', '=', False),
                ('kaynak', '=', 'e-fatura-izibiz'),
                ('company_id', '=', company.id),
            ], order='issue_date ASC', limit=BATCH_SIZE)
            try:
                client, session_id, request_header = \
                    self._get_soap_client_and_login(company)
            except Exception as e:
                _logger.error(
                    "[GUVEN-EFATURA] Login hatası [%s]: %s", company.name, e,
                )
                continue

            try:
                for idx, inv in enumerate(inv_set, 1):
                    try:
                        inv._fetch_and_parse_xml(client, request_header)
                        _logger.info(
                            "[GUVEN-EFATURA] %s başarılı [%s] (%d/%d)",
                            inv.invoice_id, company.name, idx, len(inv_set),
                        )
                    except Exception as e:
                        _logger.warning(
                            "[GUVEN-EFATURA] %s hatası [%s]: %s",
                            inv.invoice_id, company.name, e,
                        )
                        continue

                    if idx % COMMIT_EVERY == 0:
                        self.env.cr.commit()
            finally:
                try:
                    client.service.Logout(REQUEST_HEADER=request_header)
                except Exception:
                    pass

        self.env.cr.commit()

    @api.model
    def _cron_fetch_earsiv_details(self):
        """details_received=False e-arşiv kayıtların XML detayını çek ve parse et."""
        if not self._try_advisory_lock(self._LOCK_EARSIV_DETAIL):
            _logger.info("[GUVEN-EARSIV] Önceki çalışma devam ediyor, atlanıyor.")
            return

        _logger.info("[GUVEN-EARSIV] Cron başladı.")
        t0 = time.time()
        try:
            self._do_fetch_earsiv_details()
        finally:
            elapsed = time.time() - t0
            remaining = self.sudo()._read_group(
                [('details_received', '=', False), ('kaynak', '=', 'e-arsiv-izibiz')],
                groupby=['company_id'],
                aggregates=['__count'],
            )
            for company, count in remaining:
                _logger.info(
                    "[GUVEN-EARSIV] Kalan: %d fatura [%s]",
                    count, company.name if company else 'Şirketsiz',
                )
            if not remaining:
                _logger.info("[GUVEN-EARSIV] Kalan: 0 fatura (tümü tamamlandı)")
            _logger.info("[GUVEN-EARSIV] Cron bitti. Süre: %.1f sn", elapsed)
            self._release_advisory_lock(self._LOCK_EARSIV_DETAIL)

    @api.model
    def _do_fetch_earsiv_details(self):
        """E-arşiv XML detay çekme iç implementasyonu."""
        BATCH_SIZE = 100
        COMMIT_EVERY = 50

        # Bekleyen faturası olan şirketleri bul
        company_groups = self.sudo()._read_group(
            [('details_received', '=', False), ('kaynak', '=', 'e-arsiv-izibiz')],
            groupby=['company_id'],
            aggregates=['__count'],
        )
        company_ids = [company.id for company, _count in company_groups if company]
        if not company_ids:
            return

        companies = self.env['res.company'].sudo().browse(company_ids)

        for company in companies:
            # Her şirket için ayrı 100'er fatura çek
            inv_set = self.search([
                ('details_received', '=', False),
                ('kaynak', '=', 'e-arsiv-izibiz'),
                ('company_id', '=', company.id),
            ], order='issue_date ASC', limit=BATCH_SIZE)
            try:
                efatura_client, earsiv_client, session_id, request_header = \
                    self._get_earsiv_soap_client(company)
            except Exception as e:
                _logger.error(
                    "[GUVEN-EARSIV] Login hatası [%s]: %s", company.name, e,
                )
                continue

            try:
                for idx, inv in enumerate(inv_set, 1):
                    try:
                        inv._fetch_earsiv_xml(earsiv_client, request_header)
                        _logger.info(
                            "[GUVEN-EARSIV] %s başarılı [%s] (%d/%d)",
                            inv.invoice_id, company.name, idx, len(inv_set),
                        )
                    except Exception as e:
                        _logger.warning(
                            "[GUVEN-EARSIV] %s hatası [%s]: %s",
                            inv.invoice_id, company.name, e,
                        )
                        continue

                    if idx % COMMIT_EVERY == 0:
                        self.env.cr.commit()
            finally:
                try:
                    efatura_client.service.Logout(REQUEST_HEADER=request_header)
                except Exception:
                    pass

        self.env.cr.commit()

    # ==================================================================
    # CRON: HEADER SYNC (7 GÜNLÜK BLOKLAR)
    # ==================================================================

    @api.model
    def _cron_sync_headers(self):
        """Tüm şirketler için e-fatura/e-arşiv header sync (7 günlük bloklar)."""
        if not self._try_advisory_lock(self._LOCK_HEADER_SYNC):
            _logger.warning("[GUVEN-SYNC] Header sync zaten çalışıyor, atlanıyor.")
            return

        try:
            t0 = time.time()
            today = fields.Date.today()
            companies = self.env['res.company'].sudo().search([
                ('efatura_username', '!=', False),
                ('efatura_password', '!=', False),
            ])

            total_created = total_updated = 0

            for company in companies:
                lookback = company.efatura_sync_lookback_days or 3
                min_start = today - timedelta(days=lookback)

                # Bugünün turu zaten tamamlandıysa bu şirketi atla
                last_completed = company.efatura_sync_last_completed_date
                if last_completed and last_completed >= today:
                    continue

                # Cursor'ı belirle
                cursor = company.efatura_sync_cursor_date
                if not cursor:
                    # İlk çalışma — lookback başlangıcından başla
                    cursor = min_start
                elif last_completed and last_completed < today:
                    # Yeni gün, önceki tur tamamlanmıştı — yeni tur başlat
                    cursor = min_start

                # Güvenlik: lookback_days küçültüldüyse cursor'ı düzelt
                if cursor < min_start:
                    cursor = min_start

                # Cursor zaten bugüne ulaştıysa turu tamamla
                if cursor >= today:
                    company.sudo().write({
                        'efatura_sync_last_completed_date': today,
                    })
                    self.env.cr.commit()
                    continue

                block_end = min(cursor + timedelta(days=6), today)

                _logger.info(
                    "[GUVEN-SYNC] %s: %s → %s",
                    company.name, cursor, block_end,
                )

                created = updated = 0
                for direction in ('IN', 'OUT'):
                    r = self._sync_efatura_headers(cursor, block_end, direction, company)
                    created += r['created']
                    updated += r['updated']

                r = self._sync_earsiv_headers(cursor, block_end, company)
                created += r['created']
                updated += r['updated']

                total_created += created
                total_updated += updated

                # Cursor'ı ilerlet
                next_cursor = block_end + timedelta(days=1)
                write_vals = {'efatura_sync_cursor_date': next_cursor}
                if next_cursor >= today:
                    write_vals['efatura_sync_last_completed_date'] = today
                company.sudo().write(write_vals)
                # Blok sonrası commit (şirketler arası izolasyon)
                self.env.cr.commit()

                _logger.info(
                    "[GUVEN-SYNC] %s: %d yeni, %d güncellenen",
                    company.name, created, updated,
                )

            elapsed = time.time() - t0
            _logger.info(
                "[GUVEN-SYNC] Cron tamamlandı. %d yeni, %d günc. Süre: %.1f sn",
                total_created, total_updated, elapsed,
            )
        except Exception:
            _logger.exception("[GUVEN-SYNC] Cron hatası")
        finally:
            self._release_advisory_lock(self._LOCK_HEADER_SYNC)

    # ==================================================================
    # LOGO EŞLEŞTİRME
    # ==================================================================

    def _process_single_match(self, fatura, lr, stats):
        """Tek eşleşme için fark analizi yap ve guven.fatura'ya yaz."""
        # Tutar farkı
        efatura_tutar = fatura.payable_amount_try or 0.0
        logo_tutar = lr.fatura_tutari or 0.0
        farki = efatura_tutar - logo_tutar
        tutar_farki_var = abs(farki) > 0.005

        # Kimlik fark analizi (GIB VKN/TCKN aynı alanda → Logo VKN veya TCKN'ye eşitse OK)
        if fatura.direction == 'IN':
            gib_kimlik = (fatura.sender or '').strip()
        elif fatura.direction == 'OUT':
            gib_kimlik = (fatura.receiver or '').strip()
        else:
            gib_kimlik = ''

        logo_vkn = (lr.vkn or '').strip()
        logo_tckn = (lr.tckn or '').strip()

        kimlik_eslesti = (
            (logo_vkn and gib_kimlik == logo_vkn)
            or (logo_tckn and gib_kimlik == logo_tckn)
        )
        kimlik_farkli = bool(gib_kimlik and (logo_vkn or logo_tckn) and not kimlik_eslesti)

        # Tarih farkı
        logo_tarihi = lr.fatura_tarihi_1 or lr.fatura_tarihi_2
        fatura_tarihi_farkli = bool(
            fatura.issue_date and logo_tarihi
            and fatura.issue_date != logo_tarihi
        )

        if tutar_farki_var:
            stats['tutar_farki'] += 1
        if kimlik_farkli:
            stats['kimlik_farkli'] += 1
        if fatura_tarihi_farkli:
            stats['fatura_tarihi_farkli'] += 1

        fatura.write({
            'logo_fatura_ids': [(6, 0, [lr.id])],
            'logo_fatura_count': 1,
            'logo_mssql_id': lr.logo_id,
            'logo_fatura_tarihi': lr.fatura_tarihi_1 or lr.fatura_tarihi_2,
            'logo_fatura_tutari': logo_tutar,
            'logo_fatura_vkn': logo_vkn or False,
            'logo_fatura_tckn': logo_tckn or False,
            'tutar_farki_var': tutar_farki_var,
            'tutar_farki': round(farki, 2),
            'kimlik_farkli': kimlik_farkli,
            'fatura_tarihi_farkli': fatura_tarihi_farkli,
            'logo_notes': False,
        })

    @api.model
    def _match_logo_invoices(self, date_from, date_to, company_ids):
        """Logo faturaları ile e-fatura/e-arşiv kayıtlarını eşleştir.

        Args:
            date_from: Başlangıç tarihi (fields.Date)
            date_to: Bitiş tarihi (fields.Date)
            company_ids: list of int — eşleştirilecek şirket ID'leri
        """
        LogoFatura = self.env['guven.logo.fatura']
        stats = {
            'total': 0,
            'matched_single': 0,
            'matched_multi': 0,
            'unmatched': 0,
            'tutar_farki': 0,
            'kimlik_farkli': 0,
            'fatura_tarihi_farkli': 0,
        }

        # 1. İlgili guven.fatura kayıtlarını al
        faturas = self.search([
            ('issue_date', '>=', date_from),
            ('issue_date', '<=', date_to),
            ('gvn_active', '=', True),
            ('company_id', 'in', company_ids),
        ])
        if not faturas:
            return stats
        stats['total'] = len(faturas)

        # 2. İlgili şirketlerin tüm Logo kayıtlarını bir seferde yükle (N+1 önleme)
        logo_recs = LogoFatura.search([
            ('company_id', 'in', company_ids),
        ])

        # 3. Lookup dict'leri oluştur: (company_id, fatura_no) → [records]
        by_no1 = {}  # (company_id, fatura_no_1) → [logo_rec, ...]
        by_no2 = {}  # (company_id, fatura_no_2) → [logo_rec, ...]
        for lr in logo_recs:
            if lr.fatura_no_1:
                by_no1.setdefault((lr.company_id.id, lr.fatura_no_1), []).append(lr)
            if lr.fatura_no_2:
                by_no2.setdefault((lr.company_id.id, lr.fatura_no_2), []).append(lr)

        # 4. Her guven.fatura için eşleşme ara
        for fatura in faturas:
            inv_id = fatura.invoice_id
            cid = fatura.company_id.id

            # Her iki dict'te ara, sonuçları id bazlı deduplicate et
            matches_map = {}
            for lr in by_no1.get((cid, inv_id), []):
                matches_map[lr.id] = lr
            for lr in by_no2.get((cid, inv_id), []):
                matches_map[lr.id] = lr

            matches = list(matches_map.values())
            match_count = len(matches)

            if match_count == 0:
                # Eşleşme yok — Logo alanlarını temizle
                stats['unmatched'] += 1
                fatura.write({
                    'logo_fatura_ids': [(5, 0, 0)],
                    'logo_fatura_count': 0,
                    'logo_mssql_id': False,
                    'logo_fatura_tarihi': False,
                    'logo_fatura_tutari': 0.0,
                    'logo_fatura_vkn': False,
                    'logo_fatura_tckn': False,
                    'tutar_farki_var': False,
                    'tutar_farki': 0.0,
                    'kimlik_farkli': False,
                    'fatura_tarihi_farkli': False,
                    'logo_notes': False,
                })

            elif match_count == 1:
                # Tek eşleşme — detay alanlarını doldur, fark analizi yap
                stats['matched_single'] += 1
                self._process_single_match(fatura, matches[0], stats)

            else:
                # Birden fazla eşleşme — VKN ile daraltmayı dene
                gib_vkn = ''
                if fatura.direction == 'IN':
                    gib_vkn = (fatura.sender or '').strip()
                elif fatura.direction == 'OUT':
                    gib_vkn = (fatura.receiver or '').strip()

                if gib_vkn:
                    vkn_filtered = [
                        lr for lr in matches
                        if (lr.vkn or '').strip() == gib_vkn
                        or (lr.tckn or '').strip() == gib_vkn
                    ]
                else:
                    vkn_filtered = []

                if len(vkn_filtered) == 1:
                    # VKN filtresi tek eşleşmeye indirdi
                    stats['matched_single'] += 1
                    self._process_single_match(fatura, vkn_filtered[0], stats)
                else:
                    # Daraltma başarısız (0 veya 2+) — çoklu eşleşme
                    stats['matched_multi'] += 1
                    final_matches = vkn_filtered if len(vkn_filtered) > 1 else matches
                    final_count = len(final_matches)
                    lines = [f"Logoda birden fazla kayıt bulundu ({final_count} adet):"]
                    for lr in final_matches:
                        tarih = lr.fatura_tarihi_1 or lr.fatura_tarihi_2
                        tarih_str = tarih.strftime('%Y-%m-%d') if tarih else '-'
                        lines.append(
                            f"  - Logo ID: {lr.logo_id}, "
                            f"No1: {lr.fatura_no_1 or '-'}, "
                            f"No2: {lr.fatura_no_2 or '-'}, "
                            f"Tutar: {lr.fatura_tutari:.2f}, "
                            f"Tarih: {tarih_str}"
                        )

                    fatura.write({
                        'logo_fatura_ids': [(6, 0, [lr.id for lr in final_matches])],
                        'logo_fatura_count': final_count,
                        'logo_mssql_id': False,
                        'logo_fatura_tarihi': False,
                        'logo_fatura_tutari': 0.0,
                        'logo_fatura_vkn': False,
                        'logo_fatura_tckn': False,
                        'tutar_farki_var': False,
                        'tutar_farki': 0.0,
                        'kimlik_farkli': False,
                        'fatura_tarihi_farkli': False,
                        'logo_notes': '\n'.join(lines),
                    })

        return stats
