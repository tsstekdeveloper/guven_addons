"""Rename kaynak selection values: e-fatura → e-fatura-izibiz, e-arsiv → e-arsiv-izibiz."""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    _logger.info("[GUVEN-MIGRATE] Renaming kaynak values in guven_fatura and guven_logo_fatura...")

    cr.execute("UPDATE guven_fatura SET kaynak = 'e-fatura-izibiz' WHERE kaynak = 'e-fatura'")
    _logger.info("[GUVEN-MIGRATE] guven_fatura e-fatura → e-fatura-izibiz: %d rows", cr.rowcount)

    cr.execute("UPDATE guven_fatura SET kaynak = 'e-arsiv-izibiz' WHERE kaynak = 'e-arsiv'")
    _logger.info("[GUVEN-MIGRATE] guven_fatura e-arsiv → e-arsiv-izibiz: %d rows", cr.rowcount)

    cr.execute("UPDATE guven_logo_fatura SET gib_kaynak = 'e-fatura-izibiz' WHERE gib_kaynak = 'e-fatura'")
    _logger.info("[GUVEN-MIGRATE] guven_logo_fatura e-fatura → e-fatura-izibiz: %d rows", cr.rowcount)

    cr.execute("UPDATE guven_logo_fatura SET gib_kaynak = 'e-arsiv-izibiz' WHERE gib_kaynak = 'e-arsiv'")
    _logger.info("[GUVEN-MIGRATE] guven_logo_fatura e-arsiv → e-arsiv-izibiz: %d rows", cr.rowcount)

    _logger.info("[GUVEN-MIGRATE] Kaynak value migration completed.")
