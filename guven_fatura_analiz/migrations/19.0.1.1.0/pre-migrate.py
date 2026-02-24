"""Rename kaynak selection values: e-fatura → e-fatura-izibiz, e-arsiv → e-arsiv-izibiz."""
import logging

_logger = logging.getLogger(__name__)


def _column_exists(cr, table, column):
    cr.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (table, column),
    )
    return cr.fetchone() is not None


def migrate(cr, version):
    _logger.info("[GUVEN-MIGRATE] Renaming kaynak values in guven_fatura and guven_logo_fatura...")

    cr.execute("UPDATE guven_fatura SET kaynak = 'e-fatura-izibiz' WHERE kaynak = 'e-fatura'")
    _logger.info("[GUVEN-MIGRATE] guven_fatura e-fatura → e-fatura-izibiz: %d rows", cr.rowcount)

    cr.execute("UPDATE guven_fatura SET kaynak = 'e-arsiv-izibiz' WHERE kaynak = 'e-arsiv'")
    _logger.info("[GUVEN-MIGRATE] guven_fatura e-arsiv → e-arsiv-izibiz: %d rows", cr.rowcount)

    # gib_kaynak is a new column added in this version — skip if it doesn't exist yet
    if _column_exists(cr, 'guven_logo_fatura', 'gib_kaynak'):
        cr.execute("UPDATE guven_logo_fatura SET gib_kaynak = 'e-fatura-izibiz' WHERE gib_kaynak = 'e-fatura'")
        _logger.info("[GUVEN-MIGRATE] guven_logo_fatura e-fatura → e-fatura-izibiz: %d rows", cr.rowcount)

        cr.execute("UPDATE guven_logo_fatura SET gib_kaynak = 'e-arsiv-izibiz' WHERE gib_kaynak = 'e-arsiv'")
        _logger.info("[GUVEN-MIGRATE] guven_logo_fatura e-arsiv → e-arsiv-izibiz: %d rows", cr.rowcount)
    else:
        _logger.info("[GUVEN-MIGRATE] guven_logo_fatura.gib_kaynak column does not exist yet, skipping.")

    _logger.info("[GUVEN-MIGRATE] Kaynak value migration completed.")
