"""Pre-migration for guven.logo.fatura logo_firma_kodu field.

Eski unique constraint'i (logo_id, company_id) düşür — yeni 3'lü
constraint (logo_id, company_id, logo_firma_kodu) Odoo tarafından
modelden otomatik oluşturulur.

KRİTİK: Farklı Logo firma tabloları (örn. LG_550 vs LG_600) aynı
LOGICALREF değerini kullanabildiği için logo_id tek başına unique
değildir. Eski constraint, dönem geçişi olan firmalarda (Ankara Güven,
NBA Güven) aynı DB satırına farklı dönemlerin kayıtlarını yazdırdı —
her sync'te veri kaybına yol açtı.
"""


def migrate(cr, version):
    cr.execute("""
        ALTER TABLE guven_logo_fatura
        DROP CONSTRAINT IF EXISTS guven_logo_fatura_unique_logo
    """)
