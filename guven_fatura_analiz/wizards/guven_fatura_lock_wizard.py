from markupsafe import Markup

from odoo import fields, models


class GuvenFaturaLockWizard(models.TransientModel):
    _name = 'guven.fatura.lock.wizard'
    _description = 'Fatura Kilitleme Sihirbazı'

    lock_reason = fields.Text(string='Kilitleme Nedeni')
    fatura_count = fields.Integer(
        string='Fatura Sayısı', readonly=True,
    )

    def action_confirm(self):
        active_ids = self.env.context.get('active_ids', [])
        faturas = self.env['guven.fatura'].browse(active_ids)
        to_lock = faturas.filtered(lambda r: not r.is_locked)
        for record in to_lock:
            vals = {
                'is_locked': True,
                'locked_by_id': self.env.user.id,
                'locked_date': fields.Datetime.now(),
            }
            if self.lock_reason:
                vals['lock_reason'] = self.lock_reason
            record.write(vals)
            body = Markup("Kayıt <b>%s</b> tarafından kilitlendi.") % self.env.user.name
            if self.lock_reason:
                body += Markup("<br/>Neden: %s") % self.lock_reason
            record.message_post(body=body, message_type='notification')
        return {'type': 'ir.actions.client', 'tag': 'reload'}
