import logging

import pymssql

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_KDV2_SQL = """
SELECT
 AA.LOGICALREF as logoID,
 MONTH(A.DATE_) as ay,
 YEAR(A.DATE_) as yil,
 AA.FICHENO as fisNo,
 E.CODE+' '+E.NAME as proje,
 F1.CODE as kebirHesapKodu,
 F1.DEFINITION_ as kebirHesapAdi,
 F.CODE as hesapKodu,
 F.DEFINITION_ as hesapAdi,
 G.CODE+' '+G.DEFINITION_ as masrafMerkezi,
 CASE WHEN AA.MODULENR=1 THEN '1 Malzeme'
  WHEN AA.MODULENR=2 THEN '2 Satinalma'
  WHEN AA.MODULENR=3 THEN '3 Satis'
  WHEN AA.MODULENR=4 THEN '4 Cari Hesap'
  WHEN AA.MODULENR=5 THEN '5 Cek Senet'
  WHEN AA.MODULENR=6 THEN '6 Banka'
  WHEN AA.MODULENR=7 THEN '7 Kasa'
  ELSE '' END as kaynakModul,
 A.LINEEXP as aciklama,
 AA.GENEXP1 as fisAciklama,
 A.CLDEF as cari,
 CL.TAXNR as cariVergiNo,
 CL.DEFINITION_ as cariUnvan,
 CL.ADDR1 + ' ' + CL.ADDR2 as cari_adresi,
 CL.NAME as adi,
 CL.SURNAME as soyAdi,
 CL.TCKNO as tckn,
 CASE WHEN A.LOGICALREF NOT IN (SELECT DISTINCT PREVLINEREF FROM LG_{f}_01_ACCDISTDETLN) OR A1.DISTRATE=1 THEN ABS(A.DEBIT-A.CREDIT)-ABS(A.DEBIT-A.CREDIT)*2*A.SIGN ELSE A1.CREDEBNET-A1.CREDEBNET*2*A.SIGN END as tutarYerel,
 SEML.VATAMOUNT as kdvTutar,
 SEML.DEDUCTION as tevkifatOran,
 CASE WHEN ISNULL(SSTL.DEDUCTIONPART1,0) != 0
                                    AND ISNULL(SSTL.DEDUCTIONPART2,0) != 0
                                    AND ISNULL(SSTL.VAT,0) != 0
                                            THEN  ROUND((SSTL.GROSSTOTAL*SSTL.VAT/100) *  CAST(SSTL.DEDUCTIONPART1  AS FLOAT)  / CAST(SSTL.DEDUCTIONPART2 AS FLOAT),2)  ELSE 0 END as tevkifEdilenKdvTutari
FROM  LG_{f}_01_EMFLINE A WITH(NOLOCK)
 LEFT JOIN LG_{f}_01_EMFICHE AA WITH(NOLOCK) ON AA.LOGICALREF=A.ACCFICHEREF
 LEFT JOIN LG_{f}_01_ACCDISTDETLN A1 WITH(NOLOCK) ON A1.PREVLINEREF=A.LOGICALREF
 LEFT JOIN L_CAPIDIV C WITH(NOLOCK) ON C.NR=A.BRANCH AND C.FIRMNR={f}
 LEFT JOIN L_CAPIDEPT D WITH(NOLOCK) ON D.NR=A.DEPARTMENT AND D.FIRMNR={f}
 LEFT JOIN LG_{f}_PROJECT E WITH(NOLOCK) ON E.LOGICALREF=A1.PROJECTREF
 LEFT JOIN LG_{f}_EMUHACC F WITH(NOLOCK) ON F.LOGICALREF=A.ACCOUNTREF
 LEFT JOIN LG_{f}_EMUHACC F1 WITH(NOLOCK) ON F1.CODE=left(F.CODE,3)
 LEFT JOIN LG_{f}_EMCENTER G WITH(NOLOCK) ON G.LOGICALREF=A.CENTERREF
 LEFT JOIN LG_{f}_01_INVOICE N1 WITH(NOLOCK) ON N1.LOGICALREF = A.SOURCEFREF
 LEFT JOIN LG_{f}_01_INVOICE N2 WITH(NOLOCK) ON N2.ACCFICHEREF = AA.LOGICALREF
 LEFT JOIN LG_{f}_CLCARD CL WITH(NOLOCK)  ON CL.LOGICALREF = N2.CLIENTREF
 LEFT JOIN (SELECT STL.INVOICEREF, SC.CANDEDUCT, STL.DEDUCTIONPART1,STL.DEDUCTIONPART2, STL.VAT,
            CASE
            WHEN (STL.IOCODE=1 OR STL.IOCODE=2 OR STL.TRCODE=4) OR (STL.IOCODE=0 AND STL.TRCODE IN (1,3)) THEN ROUND(STL.VATAMNT,2)
            WHEN (STL.IOCODE=3 OR STL.IOCODE=4 OR STL.TRCODE=9) OR (STL.IOCODE=0 AND STL.TRCODE IN (6,8)) THEN (-1)*ROUND(STL.VATAMNT,2)
            ELSE 0
            END AS VATAMOUNT,
            CASE
              WHEN (STL.IOCODE=1 OR STL.IOCODE=2 OR STL.TRCODE=4) OR (STL.IOCODE=0 AND STL.TRCODE IN (1,3)) THEN ROUND((STL.LINENET-(STL.DISTEXP-STL.DISTDISC)),2)
              WHEN (STL.IOCODE=3 OR STL.IOCODE=4 OR STL.TRCODE=9) OR (STL.IOCODE=0 AND STL.TRCODE IN (6,8)) THEN (-1)*ROUND((STL.LINENET-(STL.DISTEXP-STL.DISTDISC)),2)
              ELSE 0
              END AS GROSSTOTAL
            FROM LG_{f}_01_STLINE STL
            JOIN LG_{f}_SRVCARD SC WITH(NOLOCK) ON SC.LOGICALREF=STL.STOCKREF
            WHERE STL.LINETYPE = 4
            AND SC.CANDEDUCT=1
            AND (STL.DEDUCTIONPART1 != 0 AND STL.DEDUCTIONPART2 !=0 )
            ) SSTL ON SSTL.INVOICEREF = N1.LOGICALREF
INNER JOIN (
            SELECT EML.ACCFICHEREF, CREDIT AS VATAMOUNT, F.CODE AS VATCODE,
                    CASE
                        WHEN F.CODE = '360.10.04.020' THEN '2/10'
                        WHEN F.CODE = '360.10.04.030' THEN '3/10'
                        WHEN F.CODE = '360.10.04.040' THEN '4/10'
                        WHEN F.CODE = '360.10.04.050' THEN '5/10'
                        WHEN F.CODE = '360.10.04.070' THEN '7/10'
                        WHEN F.CODE = '360.10.04.080' THEN '8/10'
                        WHEN F.CODE = '360.10.04.090' THEN '9/10'
                        WHEN F.CODE = '360.10.04.100' THEN '10/10'
                    END AS DEDUCTION
            FROM  LG_{f}_01_EMFLINE EML WITH(NOLOCK)
             LEFT JOIN LG_{f}_01_EMFICHE AA WITH(NOLOCK) ON AA.LOGICALREF=EML.ACCFICHEREF
             LEFT JOIN LG_{f}_01_ACCDISTDETLN A1 WITH(NOLOCK) ON A1.PREVLINEREF=EML.LOGICALREF
             LEFT JOIN LG_{f}_EMUHACC F WITH(NOLOCK) ON F.LOGICALREF=EML.ACCOUNTREF
            WHERE AA.CANCELLED = 0
            AND F.CODE LIKE '360.10.04%%'
            AND AA.MODULENR=2
            ) SEML ON SEML.ACCFICHEREF = A.ACCFICHEREF
WHERE ISNULL(AA.CANCELLED,0) = 0
AND ISNULL(A.CANCELLED,0)=0
AND (F.CODE LIKE '7%%' OR F.CODE LIKE '253%%' OR F.CODE LIKE '255%%' OR F.CODE LIKE '260%%')
AND MONTH(A.DATE_)= %s
AND YEAR(A.DATE_)= %s
AND AA.MODULENR=2
"""


class GuvenKdv2Report(models.TransientModel):
    _name = 'guven.kdv2.report'
    _description = 'KDV-2 Raporu'
    _check_company_auto = True

    company_id = fields.Many2one(
        'res.company', string='Sirket', required=True,
        default=lambda self: self.env.company, readonly=True,
    )
    logo_id = fields.Integer(string='Logo ID', readonly=True)
    ay = fields.Integer(string='Ay', readonly=True)
    yil = fields.Integer(string='Yil', readonly=True)
    fis_no = fields.Char(string='Fis No', readonly=True)
    proje = fields.Char(string='Proje', readonly=True)
    kebir_hesap_kodu = fields.Char(string='Kebir Hesap Kodu', readonly=True)
    kebir_hesap_adi = fields.Char(string='Kebir Hesap Adi', readonly=True)
    hesap_kodu = fields.Char(string='Hesap Kodu', readonly=True)
    hesap_adi = fields.Char(string='Hesap Adi', readonly=True)
    masraf_merkezi = fields.Char(string='Masraf Merkezi', readonly=True)
    kaynak_modul = fields.Char(string='Kaynak Modul', readonly=True)
    aciklama = fields.Char(string='Aciklama', readonly=True)
    fis_aciklama = fields.Char(string='Fis Aciklama', readonly=True)
    cari = fields.Char(string='Cari', readonly=True)
    cari_vergi_no = fields.Char(string='Cari Vergi No', readonly=True)
    cari_unvan = fields.Char(string='Cari Unvan', readonly=True)
    cari_adresi = fields.Char(string='Cari Adresi', readonly=True)
    adi = fields.Char(string='Adi', readonly=True)
    soy_adi = fields.Char(string='Soyadi', readonly=True)
    tckn = fields.Char(string='TCKN', readonly=True)
    tutar_yerel = fields.Float(string='Tutar (Yerel)', digits=(16, 2), readonly=True)
    kdv_tutar = fields.Float(string='KDV Tutar', digits=(16, 2), readonly=True)
    tevkifat_oran = fields.Char(string='Tevkifat Oran', readonly=True)
    tevkif_edilen_kdv_tutari = fields.Float(
        string='Tevkif Edilen KDV Tutari', digits=(16, 2), readonly=True,
    )


class GuvenKdv2Wizard(models.TransientModel):
    _name = 'guven.kdv2.wizard'
    _description = 'KDV-2 Rapor Sihirbazi'

    month = fields.Selection([
        ('1', 'Ocak'), ('2', 'Subat'), ('3', 'Mart'),
        ('4', 'Nisan'), ('5', 'Mayis'), ('6', 'Haziran'),
        ('7', 'Temmuz'), ('8', 'Agustos'), ('9', 'Eylul'),
        ('10', 'Ekim'), ('11', 'Kasim'), ('12', 'Aralik'),
    ], string='Ay', required=True, default=lambda self: str(fields.Date.today().month))

    year = fields.Integer(
        string='Yil', required=True, default=lambda self: fields.Date.today().year,
    )

    def action_generate_report(self):
        companies = self.env.companies
        if len(companies) > 1:
            raise UserError(_(
                "Birden fazla firma secili. KDV-2 raporu icin lutfen tek bir firma secin."
            ))

        company = self.env.company
        firma_kodu = company.logo_firma_kodu
        if not firma_kodu:
            raise UserError(_(
                "Secili firma icin Logo firma kodu tanimlanmamis. "
                "Lutfen Sirket Tanimlari menusunden Logo firma kodunu girin."
            ))

        # Clear previous results for this company
        self.env['guven.kdv2.report'].search([
            ('company_id', '=', company.id),
        ]).unlink()

        conn = None
        cursor = None
        try:
            creds = company.get_logo_credentials()
            conn = pymssql.connect(
                server=creds['server'],
                port=creds['port'],
                database=creds['database'],
                user=creds['username'],
                password=creds['password'],
                timeout=120,
                login_timeout=60,
                charset='cp1254',
            )
            cursor = conn.cursor(as_dict=True)

            query = _KDV2_SQL.format(f=firma_kodu)
            cursor.execute(query, (int(self.month), self.year))

            vals_list = []
            for row in cursor:
                vals_list.append({
                    'company_id': company.id,
                    'logo_id': row.get('logoID'),
                    'ay': row.get('ay'),
                    'yil': row.get('yil'),
                    'fis_no': row.get('fisNo'),
                    'proje': row.get('proje'),
                    'kebir_hesap_kodu': row.get('kebirHesapKodu'),
                    'kebir_hesap_adi': row.get('kebirHesapAdi'),
                    'hesap_kodu': row.get('hesapKodu'),
                    'hesap_adi': row.get('hesapAdi'),
                    'masraf_merkezi': row.get('masrafMerkezi'),
                    'kaynak_modul': row.get('kaynakModul'),
                    'aciklama': row.get('aciklama'),
                    'fis_aciklama': row.get('fisAciklama'),
                    'cari': row.get('cari'),
                    'cari_vergi_no': row.get('cariVergiNo'),
                    'cari_unvan': row.get('cariUnvan'),
                    'cari_adresi': row.get('cari_adresi'),
                    'adi': row.get('adi'),
                    'soy_adi': row.get('soyAdi'),
                    'tckn': row.get('tckn'),
                    'tutar_yerel': row.get('tutarYerel') or 0.0,
                    'kdv_tutar': row.get('kdvTutar') or 0.0,
                    'tevkifat_oran': row.get('tevkifatOran'),
                    'tevkif_edilen_kdv_tutari': row.get('tevkifEdilenKdvTutari') or 0.0,
                })

            if not vals_list:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Bilgi'),
                        'message': _('Secilen donem icin kayit bulunamadi.'),
                        'type': 'warning',
                        'sticky': False,
                    },
                }

            self.env['guven.kdv2.report'].create(vals_list)

            return {
                'name': _('KDV-2 Listesi - %s/%s (%s)') % (self.month, self.year, company.name),
                'type': 'ir.actions.act_window',
                'res_model': 'guven.kdv2.report',
                'view_mode': 'list',
                'domain': [('company_id', '=', company.id)],
            }

        except pymssql.Error as e:
            _logger.error("KDV-2 MSSQL baglanti hatasi: %s", e)
            raise UserError(_("Logo MSSQL baglanti hatasi: %s") % e) from e
        except Exception as e:
            _logger.error("KDV-2 rapor hatasi: %s", e)
            raise UserError(_("Rapor olusturma hatasi: %s") % e) from e
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
