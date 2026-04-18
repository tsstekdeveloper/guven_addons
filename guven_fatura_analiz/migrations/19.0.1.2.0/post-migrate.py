"""Post-migration cleanup: eski kullanılmayan kolonları DROP."""


def migrate(cr, version):
    deprecated_columns = [
        'alias',
        'user_type',
        'unit',
        'document_type',
        'register_time',
        'alias_creation_time',
        'deleted',
        'deletion_time',
        'last_synced_at',
    ]
    for col in deprecated_columns:
        cr.execute(
            f'ALTER TABLE guven_gib_mukellef DROP COLUMN IF EXISTS "{col}"'
        )
