"""Rename records shipped in noupdate blocks.

The label changed from "Kaynak Sistem Karşılaştırması" to "HBYS Sistemleri
Karşılaştırması". Menus, actions and views follow the XML on -u, but the cron
and the record rule live in noupdate="1" data, so they are renamed here.
"""
from odoo import SUPERUSER_ID, api

NEW_NAMES = {
    'guven_kaynak_karsilastirma.ir_cron_kaynak_karsilastirma':
        'HBYS Sistemleri Karşılaştırması (Pusula + Cerebrum)',
    'guven_kaynak_karsilastirma.guven_kaynak_karsilastirma_comp_rule':
        'HBYS Sistemleri Karşılaştırması: şirket kuralı',
}


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for xmlid, name in NEW_NAMES.items():
        record = env.ref(xmlid, raise_if_not_found=False)
        if record:
            record.name = name
