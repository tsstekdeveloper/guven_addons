"""Pre-migration for guven.gib.mukellef refactor.

- Tekilleştirme: her identifier için min(id) kalır
- Gereksiz ir.config_parameter kayıtlarını temizle (sync wizard cursor state)
- Eski unique constraint'i düşür
"""


def migrate(cr, version):
    # 1. Duplicate identifier'ları tekilleştir
    cr.execute("""
        DELETE FROM guven_gib_mukellef
        WHERE id NOT IN (
            SELECT MIN(id) FROM guven_gib_mukellef GROUP BY identifier
        )
    """)

    # 2. Artık kullanılmayan cursor state parametrelerini sil
    cr.execute("""
        DELETE FROM ir_config_parameter
        WHERE key LIKE 'guven_fatura_analiz.gib_mukellef_full_cursor_%'
    """)

    # 3. Eski composite unique constraint'i düşür
    cr.execute("""
        ALTER TABLE guven_gib_mukellef
        DROP CONSTRAINT IF EXISTS guven_gib_mukellef_unique_alias
    """)
