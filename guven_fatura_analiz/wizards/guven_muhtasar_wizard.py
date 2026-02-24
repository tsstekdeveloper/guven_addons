import logging

import pymssql

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Two UNION ALL blocks: MODULENR=2 (Satinalma) + MODULENR=6 (Banka)
# Parameters: 8 x %s (month, year repeated 4 times)
_MUHTASAR_SQL = """
SELECT
SEML.PAYTAX as odenecekGelirVergileri,
SEML.TAXTYPE as vergiTuru,
A.DATE_ as tarih,
MONTH(A.DATE_) as ay,
YEAR(A.DATE_) as yil,
AA.FICHENO as fisNo,
CASE
    WHEN A.TRCODE=1 THEN '1 Acilis'
    WHEN A.TRCODE=2 THEN '2 Tahsil'
    WHEN A.TRCODE=3 THEN '3 Tediye'
    WHEN A.TRCODE=4 THEN '4 Mahsup'
    WHEN A.TRCODE=5 THEN '5 Ozel'
    WHEN A.TRCODE=6 THEN '6 Kur Farki'
    WHEN A.TRCODE=7 THEN '7 Kapanis'
    ELSE ''
END as islem,
CAST(C.NR AS CHAR(3))+' '+C.NAME as isYeri,
CAST(D.NR AS CHAR(3))+' '+D.NAME as bolum,
E.CODE+' '+E.NAME as proje,
F1.CODE as kebirHesabiKodu,
F1.DEFINITION_ as kebirHesabiAdi,
F.CODE as hesapKodu,
F.DEFINITION_ as hesapAdi,
G.CODE+' '+G.DEFINITION_ as masrafMerkezi,
CASE
    WHEN AA.MODULENR=1 THEN '1 Malzeme'
    WHEN AA.MODULENR=2 THEN '2 Satinalma'
    WHEN AA.MODULENR=3 THEN '3 Satis'
    WHEN AA.MODULENR=4 THEN '4 Cari Hesap'
    WHEN AA.MODULENR=5 THEN '5 Cek Senet'
    WHEN AA.MODULENR=6 THEN '6 Banka'
    WHEN AA.MODULENR=7 THEN '7 Kasa'
    ELSE ''
END as kaynakModul,
-CASE
    WHEN A.TRCURR=0 AND (A.LOGICALREF NOT IN (SELECT DISTINCT PREVLINEREF FROM LG_{f}_01_ACCDISTDETLN) OR A1.DISTRATE=1) THEN ABS(A.DEBIT-A.CREDIT)-ABS(A.DEBIT-A.CREDIT)*2*A.SIGN
    WHEN A.TRCURR<>0 AND (A.LOGICALREF NOT IN (SELECT DISTINCT PREVLINEREF FROM LG_{f}_01_ACCDISTDETLN) OR A1.DISTRATE=1) THEN A.TRNET-A.TRNET*2*A.SIGN
    WHEN A.TRCURR=0 AND A1.DISTRATE<>100 THEN A1.CREDEBNET-A1.CREDEBNET*2*A.SIGN
    WHEN A.TRCURR<>0 AND A1.DISTRATE<>100 THEN A1.TRNET-A1.TRNET*2*A.SIGN
    ELSE 0
END  as tutar,
CASE
    WHEN A.LOGICALREF NOT IN (SELECT DISTINCT PREVLINEREF FROM LG_{f}_01_ACCDISTDETLN) OR A1.DISTRATE=1 THEN ABS(A.DEBIT-A.CREDIT)-ABS(A.DEBIT-A.CREDIT)*2*A.SIGN
    ELSE A1.CREDEBNET-A1.CREDEBNET*2*A.SIGN
END as tutarYerel,
A.LINEEXP as aciklama,
AA.GENEXP1 as fisAciklama,
CASE
    WHEN A.SIGN=0 THEN '0 Borc' WHEN A.SIGN=1 THEN '1 Alacak'
    ELSE ''
END as hareketYonu,
CASE
    WHEN A.CANCELLED=0 THEN 'Hayir'
    ELSE 'Evet'
END as iptal,
CASE AA.DOCTYPE
    WHEN 0 THEN 'Normal'
    WHEN 1 THEN 'Cost Of Sales'
    WHEN 2 THEN 'Differences Of Cost Of Sales'
    ELSE ''
END as belgeTuru,
A.CLDEF as cari,
CL.TAXNR as cariVergiNo,
CL.DEFINITION_ as cariUnvan1,
CL.DEFINITION2 as cariUnvan2,
CL.TCKNO as kimlikno,
CL.NAME as adi,
CL.SURNAME as soyadi,
N2.DOCODE as faturaBelgeNo,
N2.FICHENO as faturaNo,
CL.ADDR1 as adres1,
CL.COUNTRY as ulke,
-1 * SEML.TOTAL as vergi
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
    INNER JOIN (
                SELECT
                EML.ACCFICHEREF,
                CASE
                    WHEN F.CODE = '360.10.01.001' THEN 'UCRET GELIR VERGISI'
                    WHEN F.CODE = '360.10.01.002' THEN 'S.M MAKBUZU'
                    WHEN F.CODE = '360.10.01.003' THEN 'KIRA GELIR VERGISI'
                    WHEN F.CODE = '360.10.01.004' THEN 'GIDER PUSULASI GELIR VERGISI'
                    WHEN F.CODE = '360.10.01.005' THEN 'YURT DISI HIZMERT ALIMI GELIR VERGISI'
                    WHEN F.CODE LIKE '7%%' THEN 'VERGI'
                END as PAYTAX,
                CASE
                    WHEN F.CODE = '360.10.01.001' THEN 'UCRET GELIR VERGISI'
                    WHEN F.CODE = '360.10.01.002' THEN '022'
                    WHEN F.CODE = '360.10.01.003' THEN '041'
                    WHEN F.CODE = '360.10.01.004' THEN '156'
                    WHEN F.CODE = '360.10.01.005' THEN '279'
                    WHEN F.CODE LIKE '7%%' THEN ''
                END as TAXTYPE,
                CASE
                    WHEN EML.LOGICALREF NOT IN (SELECT DISTINCT PREVLINEREF FROM LG_{f}_01_ACCDISTDETLN) OR A1.DISTRATE=1 THEN ABS(EML.DEBIT-EML.CREDIT)-ABS(EML.DEBIT-EML.CREDIT)*2*EML.SIGN
                    ELSE A1.CREDEBNET-A1.CREDEBNET*2*EML.SIGN
                END AS TOTAL
                FROM  LG_{f}_01_EMFLINE EML WITH(NOLOCK)
                    LEFT JOIN LG_{f}_01_EMFICHE AA WITH(NOLOCK) ON AA.LOGICALREF=EML.ACCFICHEREF
                    LEFT JOIN LG_{f}_01_ACCDISTDETLN A1 WITH(NOLOCK) ON A1.PREVLINEREF=EML.LOGICALREF
                    LEFT JOIN LG_{f}_EMUHACC F WITH(NOLOCK) ON F.LOGICALREF=EML.ACCOUNTREF
                WHERE AA.CANCELLED = 0
                    AND (F.CODE LIKE '360.10.01%%')
                    AND MONTH(EML.DATE_)= %s
                    AND YEAR(EML.DATE_)= %s
                    and AA.MODULENR=2
                ) SEML ON  SEML.ACCFICHEREF = A.ACCFICHEREF
WHERE AA.CANCELLED = 0
    AND (F.CODE LIKE '7%%')
    AND MONTH(A.DATE_)= %s
    AND YEAR(A.DATE_)= %s
    and AA.MODULENR=2
UNION ALL
SELECT
SEML.PAYTAX as odenecekGelirVergileri,
SEML.TAXTYPE as vergiTuru,
A.DATE_ as tarih,
MONTH(A.DATE_) as ay,
YEAR(A.DATE_) as yil,
AA.FICHENO as fisNo,
CASE
    WHEN A.TRCODE=1 THEN '1 Acilis'
    WHEN A.TRCODE=2 THEN '2 Tahsil'
    WHEN A.TRCODE=3 THEN '3 Tediye'
    WHEN A.TRCODE=4 THEN '4 Mahsup'
    WHEN A.TRCODE=5 THEN '5 Ozel'
    WHEN A.TRCODE=6 THEN '6 Kur Farki'
    WHEN A.TRCODE=7 THEN '7 Kapanis'
    ELSE ''
END as islem,
CAST(C.NR AS CHAR(3))+' '+C.NAME as isYeri,
CAST(D.NR AS CHAR(3))+' '+D.NAME as bolum,
E.CODE+' '+E.NAME as proje,
F1.CODE as kebirHesabiKodu,
F1.DEFINITION_ as kebirHesabiAdi,
F.CODE as hesapKodu,
F.DEFINITION_ as hesapAdi,
G.CODE+' '+G.DEFINITION_ as masrafMerkezi,
CASE
    WHEN AA.MODULENR=1 THEN '1 Malzeme'
    WHEN AA.MODULENR=2 THEN '2 Satinalma'
    WHEN AA.MODULENR=3 THEN '3 Satis'
    WHEN AA.MODULENR=4 THEN '4 Cari Hesap'
    WHEN AA.MODULENR=5 THEN '5 Cek Senet'
    WHEN AA.MODULENR=6 THEN '6 Banka'
    WHEN AA.MODULENR=7 THEN '7 Kasa'
    ELSE ''
END as kaynakModul,
-CASE
    WHEN A.TRCURR=0 AND (A.LOGICALREF NOT IN (SELECT DISTINCT PREVLINEREF FROM LG_{f}_01_ACCDISTDETLN) OR A1.DISTRATE=1) THEN ABS(A.DEBIT-A.CREDIT)-ABS(A.DEBIT-A.CREDIT)*2*A.SIGN
    WHEN A.TRCURR<>0 AND (A.LOGICALREF NOT IN (SELECT DISTINCT PREVLINEREF FROM LG_{f}_01_ACCDISTDETLN) OR A1.DISTRATE=1) THEN A.TRNET-A.TRNET*2*A.SIGN
    WHEN A.TRCURR=0 AND A1.DISTRATE<>100 THEN A1.CREDEBNET-A1.CREDEBNET*2*A.SIGN
    WHEN A.TRCURR<>0 AND A1.DISTRATE<>100 THEN A1.TRNET-A1.TRNET*2*A.SIGN
    ELSE 0
END as tutar,
CASE
    WHEN A.LOGICALREF NOT IN (SELECT DISTINCT PREVLINEREF FROM LG_{f}_01_ACCDISTDETLN) OR A1.DISTRATE=1 THEN ABS(A.DEBIT-A.CREDIT)-ABS(A.DEBIT-A.CREDIT)*2*A.SIGN
    ELSE A1.CREDEBNET-A1.CREDEBNET*2*A.SIGN
END as tutarYerel,
A.LINEEXP as aciklama,
AA.GENEXP1 as fisAciklama,
CASE
    WHEN A.SIGN=0 THEN '0 Borc'
    WHEN A.SIGN=1 THEN '1 Alacak'
    ELSE ''
END as hareketYonu,
CASE
    WHEN A.CANCELLED=0 THEN 'Hayir'
    ELSE 'Evet'
END  as iptal,
CASE AA.DOCTYPE
    WHEN 0 THEN 'Normal'
    WHEN 1 THEN 'Cost Of Sales'
    WHEN 2 THEN 'Differences Of Cost Of Sales'
    ELSE ''
END as belgeTuru,
A.CLDEF as cari,
CL.TAXNR as cariVergiNo,
CL.DEFINITION_ as cariUnvan1,
CL.DEFINITION2 as cariUnvan2,
CL.TCKNO as kimlikno,
CL.NAME as adi,
CL.SURNAME as soyadi,
N2.DOCODE as faturaBelgeNo,
N2.FICHENO as faturaNo,
CL.ADDR1 as adres1,
CL.COUNTRY as ulke,
-1 * SEML.TOTAL as vergi
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
    INNER JOIN LG_{f}_CLCARD CL WITH(NOLOCK)  ON CL.LOGICALREF = N2.CLIENTREF
    INNER JOIN (
                SELECT
                EML.ACCFICHEREF,
                CASE
                    WHEN F.CODE = '360.10.01.001' THEN 'UCRET GELIR VERGISI'
                    WHEN F.CODE = '360.10.01.002' THEN 'S.M MAKBUZU'
                    WHEN F.CODE = '360.10.01.003' THEN 'KIRA GELIR VERGISI'
                    WHEN F.CODE = '360.10.01.004' THEN 'GIDER PUSULASI GELIR VERGISI'
                    WHEN F.CODE = '360.10.01.005' THEN 'YURT DISI HIZMERT ALIMI GELIR VERGISI'
                    WHEN F.CODE LIKE '7%%' THEN 'VERGI'
                END as PAYTAX,
                CASE
                    WHEN F.CODE = '360.10.01.001' THEN 'UCRET GELIR VERGISI'
                    WHEN F.CODE = '360.10.01.002' THEN '022'
                    WHEN F.CODE = '360.10.01.003' THEN '041'
                    WHEN F.CODE = '360.10.01.004' THEN '156'
                    WHEN F.CODE = '360.10.01.005' THEN '279'
                    WHEN F.CODE LIKE '7%%' THEN ''
                END as TAXTYPE,
                CASE
                    WHEN EML.LOGICALREF NOT IN (SELECT DISTINCT PREVLINEREF FROM LG_{f}_01_ACCDISTDETLN) OR A1.DISTRATE=1 THEN ABS(EML.DEBIT-EML.CREDIT)-ABS(EML.DEBIT-EML.CREDIT)*2*EML.SIGN
                    ELSE A1.CREDEBNET-A1.CREDEBNET*2*EML.SIGN
                END AS TOTAL
                FROM  LG_{f}_01_EMFLINE EML WITH(NOLOCK)
                    LEFT JOIN LG_{f}_01_EMFICHE AA WITH(NOLOCK) ON AA.LOGICALREF=EML.ACCFICHEREF
                    LEFT JOIN LG_{f}_01_ACCDISTDETLN A1 WITH(NOLOCK) ON A1.PREVLINEREF=EML.LOGICALREF
                    LEFT JOIN LG_{f}_EMUHACC F WITH(NOLOCK) ON F.LOGICALREF=EML.ACCOUNTREF
                WHERE AA.CANCELLED = 0
                    AND (F.CODE LIKE '360.10.01%%')
                    AND MONTH(EML.DATE_)= %s
                    AND YEAR(EML.DATE_)= %s
                    AND AA.MODULENR=6
                ) SEML ON  SEML.ACCFICHEREF = A.ACCFICHEREF
WHERE AA.CANCELLED = 0
    AND (F.CODE LIKE '740.YU[PM]%%' OR F.CODE LIKE '770.10.08.001')
    AND MONTH(A.DATE_)= %s
    AND YEAR(A.DATE_)= %s
    AND AA.MODULENR=6
"""


class GuvenMuhtasarReport(models.TransientModel):
    _name = 'guven.muhtasar.report'
    _description = 'Muhtasar Raporu'
    _check_company_auto = True

    company_id = fields.Many2one(
        'res.company', string='Sirket', required=True,
        default=lambda self: self.env.company, readonly=True,
    )
    odenecek_gelir_vergileri = fields.Char(string='Odenecek Gelir Vergileri', readonly=True)
    vergi_turu = fields.Char(string='Vergi Turu', readonly=True)
    tarih = fields.Date(string='Tarih', readonly=True)
    ay = fields.Integer(string='Ay', readonly=True)
    yil = fields.Integer(string='Yil', readonly=True)
    fis_no = fields.Char(string='Fis No', readonly=True)
    islem = fields.Char(string='Islem', readonly=True)
    is_yeri = fields.Char(string='Is Yeri', readonly=True)
    bolum = fields.Char(string='Bolum', readonly=True)
    proje = fields.Char(string='Proje', readonly=True)
    kebir_hesabi_kodu = fields.Char(string='Kebir Hesabi Kodu', readonly=True)
    kebir_hesabi_adi = fields.Char(string='Kebir Hesabi Adi', readonly=True)
    hesap_kodu = fields.Char(string='Hesap Kodu', readonly=True)
    hesap_adi = fields.Char(string='Hesap Adi', readonly=True)
    masraf_merkezi = fields.Char(string='Masraf Merkezi', readonly=True)
    kaynak_modul = fields.Char(string='Kaynak Modul', readonly=True)
    tutar = fields.Float(string='Tutar', digits=(16, 2), readonly=True)
    tutar_yerel = fields.Float(string='Tutar (Yerel)', digits=(16, 2), readonly=True)
    vergi = fields.Float(string='Vergi', digits=(16, 2), readonly=True)
    aciklama = fields.Char(string='Aciklama', readonly=True)
    fis_aciklama = fields.Char(string='Fis Aciklama', readonly=True)
    hareket_yonu = fields.Char(string='Hareket Yonu', readonly=True)
    iptal = fields.Char(string='Iptal', readonly=True)
    belge_turu = fields.Char(string='Belge Turu', readonly=True)
    cari = fields.Char(string='Cari', readonly=True)
    cari_vergi_no = fields.Char(string='Cari Vergi No', readonly=True)
    cari_unvan1 = fields.Char(string='Cari Unvan 1', readonly=True)
    cari_unvan2 = fields.Char(string='Cari Unvan 2', readonly=True)
    kimlik_no = fields.Char(string='Kimlik No', readonly=True)
    adi = fields.Char(string='Adi', readonly=True)
    soyadi = fields.Char(string='Soyadi', readonly=True)
    fatura_belge_no = fields.Char(string='Fatura Belge No', readonly=True)
    fatura_no = fields.Char(string='Fatura No', readonly=True)
    adres1 = fields.Char(string='Adres', readonly=True)
    ulke = fields.Char(string='Ulke', readonly=True)


class GuvenMuhtasarWizard(models.TransientModel):
    _name = 'guven.muhtasar.wizard'
    _description = 'Muhtasar Rapor Sihirbazi'

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
                "Birden fazla firma secili. Muhtasar raporu icin lutfen tek bir firma secin."
            ))

        company = self.env.company
        firma_kodu = company.logo_firma_kodu
        if not firma_kodu:
            raise UserError(_(
                "Secili firma icin Logo firma kodu tanimlanmamis. "
                "Lutfen Sirket Tanimlari menusunden Logo firma kodunu girin."
            ))

        # Clear previous results for this company
        self.env['guven.muhtasar.report'].search([
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

            query = _MUHTASAR_SQL.format(f=firma_kodu)
            month_int = int(self.month)
            # 8 params: (month, year) x 4 — each UNION block has 2 in SEML subquery + 2 in WHERE
            params = (month_int, self.year) * 4
            cursor.execute(query, params)

            vals_list = []
            for row in cursor:
                vals_list.append({
                    'company_id': company.id,
                    'odenecek_gelir_vergileri': row.get('odenecekGelirVergileri'),
                    'vergi_turu': row.get('vergiTuru'),
                    'tarih': row.get('tarih'),
                    'ay': row.get('ay'),
                    'yil': row.get('yil'),
                    'fis_no': row.get('fisNo'),
                    'islem': row.get('islem'),
                    'is_yeri': row.get('isYeri'),
                    'bolum': row.get('bolum'),
                    'proje': row.get('proje'),
                    'kebir_hesabi_kodu': row.get('kebirHesabiKodu'),
                    'kebir_hesabi_adi': row.get('kebirHesabiAdi'),
                    'hesap_kodu': row.get('hesapKodu'),
                    'hesap_adi': row.get('hesapAdi'),
                    'masraf_merkezi': row.get('masrafMerkezi'),
                    'kaynak_modul': row.get('kaynakModul'),
                    'tutar': row.get('tutar') or 0.0,
                    'tutar_yerel': row.get('tutarYerel') or 0.0,
                    'aciklama': row.get('aciklama'),
                    'fis_aciklama': row.get('fisAciklama'),
                    'hareket_yonu': row.get('hareketYonu'),
                    'iptal': row.get('iptal'),
                    'belge_turu': row.get('belgeTuru'),
                    'cari': row.get('cari'),
                    'cari_vergi_no': row.get('cariVergiNo'),
                    'cari_unvan1': row.get('cariUnvan1'),
                    'cari_unvan2': row.get('cariUnvan2'),
                    'kimlik_no': row.get('kimlikno'),
                    'adi': row.get('adi'),
                    'soyadi': row.get('soyadi'),
                    'fatura_belge_no': row.get('faturaBelgeNo'),
                    'fatura_no': row.get('faturaNo'),
                    'adres1': row.get('adres1'),
                    'ulke': row.get('ulke'),
                    'vergi': row.get('vergi') or 0.0,
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

            self.env['guven.muhtasar.report'].create(vals_list)

            return {
                'name': _('Muhtasar Listesi - %s/%s (%s)') % (self.month, self.year, company.name),
                'type': 'ir.actions.act_window',
                'res_model': 'guven.muhtasar.report',
                'view_mode': 'list',
                'domain': [('company_id', '=', company.id)],
            }

        except pymssql.Error as e:
            _logger.error("Muhtasar MSSQL baglanti hatasi: %s", e)
            raise UserError(_("Logo MSSQL baglanti hatasi: %s") % e) from e
        except Exception as e:
            _logger.error("Muhtasar rapor hatasi: %s", e)
            raise UserError(_("Rapor olusturma hatasi: %s") % e) from e
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
