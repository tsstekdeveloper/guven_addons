from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


def parse_id_list(text):
    """'1, 8' -> {1, 8}; raises ValueError on anything but integers."""
    ids = set()
    for part in (text or '').replace(';', ',').split(','):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise ValueError(part)
        ids.add(int(part))
    return ids


class ResCompany(models.Model):
    _inherit = 'res.company'

    # Mapping is kept by hand: Sistem.Sube has no VKN or company link, and the
    # same id names different companies in Pusula and Cerebrum (e.g. 234).
    kaynak_pusula_branch_ids = fields.Char(
        string='Pusula Şube Kimlikleri',
        groups='base.group_system',
        help="Bu şirkete ait Pusula Sistem.Sube kimlikleri, virgülle ayrılmış (örn. 10, 11).",
    )
    kaynak_cerebrum_company_ids = fields.Char(
        string='Cerebrum Şirket Kimlikleri',
        groups='base.group_system',
        help="Bu şirkete ait Cerebrum FIRMALARIM kimlikleri, virgülle ayrılmış (örn. 1, 8).",
    )

    @api.constrains('kaynak_pusula_branch_ids', 'kaynak_cerebrum_company_ids')
    def _check_kaynak_scope(self):
        for company in self:
            for value in (company.kaynak_pusula_branch_ids, company.kaynak_cerebrum_company_ids):
                try:
                    parse_id_list(value)
                except ValueError as exc:
                    raise ValidationError(
                        _("Kaynak sistem kimlikleri yalnızca virgülle ayrılmış sayılar olabilir: %s", exc)
                    ) from exc

    def _kaynak_scope(self):
        """(Pusula branch ids, Cerebrum company ids) of this company."""
        self.ensure_one()
        company = self.sudo()
        return (parse_id_list(company.kaynak_pusula_branch_ids),
                parse_id_list(company.kaynak_cerebrum_company_ids))
