"""Post-migration: mevcut Logo kayıtlarına logo_firma_kodu ata.

Her kayıt için guven_logo_donem tablosundan tarih eşleşmesi ile
firma kodu çıkarılır. Dönem geçişi olan firmalar (Ankara, NBA) dışında
kalan firmalarda tek dönem olduğu için basit eşleşme yeterlidir.

Not: Dönem geçişli firmalarda mevcut DB'de yanlış veri de olabilir
(eski bug nedeniyle). Cron cursor 2021-01-01'e sıfırlanmış durumda;
sonraki cron turlarında eksik dönem kayıtları yeniden eklenecek ve
doğru firma kodu ile saklanacak.
"""


def migrate(cr, version):
    # logo_firma_kodu'nu guven_logo_donem'den tarih eşleşmesi ile ata.
    # fatura_tarihi_1 önceliği (DATE_ alanı), yoksa fatura_tarihi_2 (DOCDATE).
    cr.execute("""
        UPDATE guven_logo_fatura lf
        SET logo_firma_kodu = d.logo_firma_kodu
        FROM guven_logo_donem d
        WHERE lf.company_id = d.company_id
          AND lf.logo_firma_kodu IS NULL
          AND COALESCE(lf.fatura_tarihi_1, lf.fatura_tarihi_2)
              BETWEEN d.baslangic_tarihi
                  AND COALESCE(d.bitis_tarihi, DATE '2099-12-31')
    """)

    # Tarih eşleşmemiş kalan kayıtlar için company üzerindeki default
    # firma kodunu kullan (tek dönemli firmalar için de yedek).
    cr.execute("""
        UPDATE guven_logo_fatura lf
        SET logo_firma_kodu = c.logo_firma_kodu
        FROM res_company c
        WHERE lf.company_id = c.id
          AND lf.logo_firma_kodu IS NULL
          AND c.logo_firma_kodu IS NOT NULL
    """)
