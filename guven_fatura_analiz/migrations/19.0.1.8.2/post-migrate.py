"""Post-migration: cron'ları aktif et ve interval'i 1 dakikaya çek.

cron_data.xml `noupdate="1"` olduğu için modül upgrade ile mevcut ir_cron
kayıtlarındaki active/interval alanları otomatik güncellenmez. Bu migration
4 cron'u aktif eder ve interval'i 1 dakika yapar.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE ir_cron
        SET active = TRUE,
            interval_number = 1,
            interval_type = 'minutes'
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'guven_fatura_analiz'
              AND name IN (
                'ir_cron_sync_headers',
                'ir_cron_logo_sync',
                'ir_cron_fetch_invoice_details',
                'ir_cron_fetch_earsiv_details'
              )
        )
    """)
