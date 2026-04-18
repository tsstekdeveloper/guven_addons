import logging
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class GuvenGibMukellefSyncWizard(models.TransientModel):
    _name = 'guven.gib.mukellef.sync.wizard'
    _description = 'GİB Mükellef Kayıtları Senkronizasyonu'

    sync_mode = fields.Selection(
        [
            ('full', 'Tamamı (tüm liste)'),
            ('delta', 'Devamı (son sync sonrası değişenler)'),
        ],
        string='Mod',
        required=True,
        default='delta',
    )
    document_type = fields.Selection(
        [
            ('ALL', 'Hepsi'),
            ('INVOICE', 'E-Fatura'),
            ('DESPATCHADVICE', 'E-İrsaliye'),
        ],
        string='Doküman Tipi',
        required=True,
        default='ALL',
    )
    last_sync_date = fields.Datetime(
        string='Son Sync Tarihi',
        compute='_compute_last_sync_date',
    )
    max_iterations = fields.Integer(
        string='Maksimum İterasyon',
        default=200,
        help='izibiz her çağrıda 100 kayıt döner. Bu sayı kadar çağrı yapılır '
             '(örn. 200 x 100 = 20.000 kayıt). Çalışma timeout olursa tekrar '
             'tetikleyin; kaldığı yerden devam eder.',
    )
    restart_from_beginning = fields.Boolean(
        string='Baştan Başla',
        default=False,
        help='Tamamı modunda işaretlenirse, saklı cursor sıfırlanır ve '
             'REGISTER_TIME_START = 2013-01-01 olarak başlar. Normalde bir '
             'önceki session kaldığı yerden devam eder.',
    )
    current_full_cursor = fields.Datetime(
        string='Mevcut Tam Sync Cursor',
        compute='_compute_current_full_cursor',
        help='Tam sync bir sonraki tetiklemede buradan başlayacak',
    )

    state = fields.Selection(
        [('draft', 'Bekliyor'), ('done', 'Tamamlandı')],
        string='Durum',
        default='draft',
    )

    total_fetched = fields.Integer(string='Çekilen', readonly=True)
    total_created = fields.Integer(string='Yeni', readonly=True)
    total_updated = fields.Integer(string='Güncellenen', readonly=True)
    total_deleted_mark = fields.Integer(string='Silinmiş İşaretlenen', readonly=True)
    total_iterations = fields.Integer(string='Yapılan İterasyon', readonly=True)
    finished = fields.Boolean(
        string='Tamamlandı (Son Sayfa)',
        readonly=True,
        help='True: tüm liste çekildi. False: max iterasyon sınırına ulaşıldı, '
             'tekrar çalıştırarak devam edin.',
    )
    log_messages = fields.Text(string='İşlem Logları', readonly=True)
    report_html = fields.Html(
        string='Rapor',
        compute='_compute_report_html',
        sanitize=False,
    )

    # Config parameter keys
    _CURSOR_KEY = 'guven_fatura_analiz.gib_mukellef_full_cursor'

    # ── Computed ─────────────────────────────────────────────────

    @api.depends_context('uid')
    def _compute_last_sync_date(self):
        Mukellef = self.env['guven.gib.mukellef'].sudo()
        self.env.cr.execute(
            "SELECT MAX(last_synced_at) FROM guven_gib_mukellef"
        )
        row = self.env.cr.fetchone()
        last_date = row[0] if row and row[0] else False
        for rec in self:
            rec.last_sync_date = last_date

    @api.depends_context('uid')
    def _compute_current_full_cursor(self):
        cursor_str = self.env['ir.config_parameter'].sudo().get_param(
            self._CURSOR_KEY,
        )
        cursor_dt = False
        if cursor_str:
            try:
                cursor_dt = fields.Datetime.from_string(cursor_str)
            except Exception:
                cursor_dt = False
        for rec in self:
            rec.current_full_cursor = cursor_dt

    @api.depends(
        'state', 'total_fetched', 'total_created', 'total_updated',
        'total_deleted_mark', 'total_iterations', 'finished',
        'sync_mode', 'document_type',
    )
    def _compute_report_html(self):
        for rec in self:
            if rec.state != 'done':
                rec.report_html = False
                continue
            rec.report_html = rec._build_report_html()

    # ── Main Action ──────────────────────────────────────────────

    def action_sync(self):
        self.ensure_one()
        log_lines = []

        # 1. Ankara Güven Hastanesi credential'larıyla SOAP client
        company = self.env['res.company'].sudo().search(
            [('name', 'ilike', 'ANKARA GÜVEN')], limit=1,
        )
        if not company:
            raise UserError(_(
                "Ankara Güven Hastanesi şirket kaydı bulunamadı."
            ))
        if not company.has_efatura_credentials():
            raise UserError(_(
                "Ankara Güven Hastanesi için E-Fatura SOAP kimlik bilgileri "
                "tanımlı değil."
            ))
        log_lines.append(f"Credential sahibi: {company.name}")

        GibFatura = self.env['guven.fatura']
        client, session_id, request_header = \
            GibFatura._get_soap_client_and_login(company)

        try:
            # 2. Cursor pagination: izibiz her çağrıda max 100 kayıt döner
            # Strateji: REGISTER_TIME_START'ı iteratif ilerlet
            doc_type = self.document_type if self.document_type != 'ALL' else None

            # Başlangıç cursor'ı belirle
            ICPSudo = self.env['ir.config_parameter'].sudo()
            if self.sync_mode == 'full':
                # "Tamamı" modu — saklı cursor'dan devam et, yoksa 2013'ten başla
                if self.restart_from_beginning:
                    ICPSudo.set_param(self._CURSOR_KEY, False)
                    cursor = datetime(2013, 1, 1)
                    log_lines.append(
                        "Tam sync: BAŞTAN başlanıyor (cursor sıfırlandı)"
                    )
                else:
                    cursor_str = ICPSudo.get_param(self._CURSOR_KEY)
                    if cursor_str:
                        try:
                            cursor = fields.Datetime.from_string(cursor_str)
                        except Exception:
                            cursor = datetime(2013, 1, 1)
                    else:
                        cursor = datetime(2013, 1, 1)
                    log_lines.append(
                        f"Tam sync: cursor başlangıcı "
                        f"{cursor.strftime('%Y-%m-%d %H:%M:%S')}"
                    )
            else:
                # Delta: last_synced_at'ten başla (fallback: son 7 gün)
                cursor = self.last_sync_date or (
                    fields.Datetime.now() - timedelta(days=7)
                )
                log_lines.append(
                    f"Delta sync: cursor başlangıcı "
                    f"{cursor.strftime('%Y-%m-%d %H:%M:%S')}"
                )
            log_lines.append(f"DOCUMENT_TYPE = {doc_type or 'ALL (hepsi)'}")
            log_lines.append(f"Max iterasyon: {self.max_iterations}")

            # Transport timeout (her iterasyon ~12sn)
            client.transport.load_timeout = 300
            client.transport.operation_timeout = 300

            totals = {'fetched': 0, 'created': 0, 'updated': 0, 'deleted_mark': 0}
            iteration = 0
            finished = False
            last_cursor = cursor

            while iteration < self.max_iterations:
                iteration += 1
                soap_args = {
                    'REQUEST_HEADER': request_header,
                    'REGISTER_TIME_START': cursor,
                }
                if doc_type:
                    soap_args['DOCUMENT_TYPE'] = doc_type

                _logger.info(
                    "[GUVEN-MUKELLEF] İterasyon %s/%s, cursor=%s",
                    iteration, self.max_iterations,
                    cursor.strftime('%Y-%m-%d %H:%M:%S'),
                )
                with client.settings(raw_response=True):
                    raw = client.service.GetUserList(**soap_args)

                user_records = self._parse_users(raw.content)
                if not user_records:
                    finished = True
                    log_lines.append(
                        f"[İt-{iteration}] Boş cevap, bittik."
                    )
                    break

                # Cursor ilerlet: gelen kayıtların en büyük register_time'ı + 1sn
                max_reg = max(
                    (r['register_time'] for r in user_records
                     if r.get('register_time')),
                    default=None,
                )

                stats = self._upsert_records(user_records)
                for key in totals:
                    totals[key] += stats[key]

                log_lines.append(
                    f"[İt-{iteration}] {stats['fetched']} kayıt → "
                    f"{stats['created']} yeni, {stats['updated']} günc., "
                    f"cursor→{max_reg.strftime('%Y-%m-%d %H:%M:%S') if max_reg else '?'}"
                )

                # Her 20 iterasyonda bir ara commit (güvenli checkpoint)
                if iteration % 20 == 0:
                    self.env.cr.commit()

                # 100'den az geldiyse son sayfa, bittik
                if len(user_records) < 100:
                    finished = True
                    break

                # Cursor'ı ilerlet
                if max_reg is None:
                    finished = True
                    log_lines.append(
                        "[Uyarı] Kayıtlarda register_time yok, "
                        "cursor ilerletilemiyor."
                    )
                    break
                new_cursor = max_reg + timedelta(seconds=1)
                if new_cursor <= last_cursor:
                    log_lines.append(
                        "[Uyarı] Cursor ilerlemiyor, döngü kırılıyor."
                    )
                    break
                cursor = new_cursor
                last_cursor = cursor

                # Full modda cursor'ı kalıcı olarak sakla (checkpoint)
                if self.sync_mode == 'full':
                    ICPSudo.set_param(
                        self._CURSOR_KEY,
                        fields.Datetime.to_string(cursor),
                    )

            # Full modda bitim durumunu yansıt
            if self.sync_mode == 'full':
                if finished:
                    # Tüm liste çekildi → cursor'ı sıfırla
                    ICPSudo.set_param(self._CURSOR_KEY, False)
                else:
                    # Max iterasyon → son cursor'ı sakla
                    ICPSudo.set_param(
                        self._CURSOR_KEY,
                        fields.Datetime.to_string(cursor),
                    )

            log_lines.append("")
            log_lines.append(
                f"TOPLAM: {totals['fetched']} çekildi, {totals['created']} yeni, "
                f"{totals['updated']} güncellenen, {iteration} iterasyon"
            )
            if finished:
                log_lines.append("✓ Sync tamamlandı (tüm veri çekildi)")
            else:
                log_lines.append(
                    "⚠ Max iterasyon sınırına ulaşıldı. "
                    "Tekrar çalıştırın — kaldığı yerden devam edecek."
                )

            # Wizard state güncelle
            self.write({
                'state': 'done',
                'total_fetched': totals['fetched'],
                'total_created': totals['created'],
                'total_updated': totals['updated'],
                'total_deleted_mark': totals['deleted_mark'],
                'total_iterations': iteration,
                'finished': finished,
                'log_messages': '\n'.join(log_lines),
            })
            _logger.info(
                "[GUVEN-MUKELLEF] Sync tamamlandı: %s iterasyon, "
                "%s yeni, %s güncellenen, finished=%s",
                iteration, totals['created'], totals['updated'], finished,
            )
        except Exception as e:
            _logger.exception("[GUVEN-MUKELLEF] Sync hatası")
            raise UserError(_("Mükellef sync hatası: %s") % str(e))
        finally:
            try:
                client.service.Logout(REQUEST_HEADER=request_header)
            except Exception:
                pass

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'context': {'dialog_size': 'extra-large'},
        }

    def action_close(self):
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    # ── Helpers ──────────────────────────────────────────────────

    def _parse_users(self, xml_bytes):
        """USER elementlerini dict listesi olarak döndür."""
        root = ET.fromstring(xml_bytes)
        users = []
        for user_elem in root.iter():
            tag = user_elem.tag.split('}')[-1] if '}' in user_elem.tag else user_elem.tag
            if tag != 'USER':
                continue

            vals = {}
            for child in user_elem:
                child_tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                text = (child.text or '').strip() or False
                if child_tag == 'IDENTIFIER':
                    vals['identifier'] = text
                elif child_tag == 'ALIAS':
                    vals['alias'] = text
                elif child_tag == 'TITLE':
                    vals['title'] = text
                elif child_tag == 'TYPE':
                    vals['user_type'] = text if text in ('OZEL', 'KAMU') else False
                elif child_tag == 'UNIT':
                    vals['unit'] = text if text in ('GB', 'PK') else False
                elif child_tag == 'DOCUMENT_TYPE':
                    vals['document_type'] = text if text in ('INVOICE', 'DESPATCHADVICE') else False
                elif child_tag == 'REGISTER_TIME':
                    vals['register_time'] = self._parse_dt(text)
                elif child_tag == 'ALIAS_CREATION_TIME':
                    vals['alias_creation_time'] = self._parse_dt(text)
                elif child_tag == 'DELETED':
                    vals['deleted'] = (text == 'Y')
                elif child_tag == 'DELETION_TIME':
                    vals['deletion_time'] = self._parse_dt(text)

            # Zorunlu alanlar
            if not vals.get('identifier') or not vals.get('alias'):
                continue
            vals.setdefault('deleted', False)
            users.append(vals)

        return users

    @staticmethod
    def _parse_dt(text):
        if not text:
            return False
        for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return False

    def _upsert_records(self, user_records):
        """identifier+alias+document_type unique key üzerinden upsert."""
        if not user_records:
            return {'fetched': 0, 'created': 0, 'updated': 0, 'deleted_mark': 0}

        now = fields.Datetime.now()
        Mukellef = self.env['guven.gib.mukellef'].sudo()

        # Mevcut kayıtları tek seferde yükle
        identifiers = list({r['identifier'] for r in user_records})
        existing = Mukellef.search([('identifier', 'in', identifiers)])
        by_key = {
            (r.identifier, r.alias, r.document_type): r
            for r in existing
        }

        created = updated = deleted_mark = 0
        to_create = []
        for vals in user_records:
            key = (
                vals.get('identifier'),
                vals.get('alias'),
                vals.get('document_type'),
            )
            existing_rec = by_key.get(key)
            if existing_rec:
                changed = {}
                for field_name, new_value in vals.items():
                    if existing_rec[field_name] != new_value:
                        changed[field_name] = new_value
                if changed:
                    changed['last_synced_at'] = now
                    existing_rec.write(changed)
                    updated += 1
                    if changed.get('deleted') is True:
                        deleted_mark += 1
            else:
                vals['last_synced_at'] = now
                to_create.append(vals)

        if to_create:
            Mukellef.create(to_create)
            created = len(to_create)

        return {
            'fetched': len(user_records),
            'created': created,
            'updated': updated,
            'deleted_mark': deleted_mark,
        }

    # ── HTML Report ──────────────────────────────────────────────

    @staticmethod
    def _fmt(num):
        return f"{num:,}".replace(",", ".")

    def _build_report_html(self):
        fmt = self._fmt
        mode_label = dict(self._fields['sync_mode'].selection).get(self.sync_mode, '-')
        doc_label = dict(self._fields['document_type'].selection).get(self.document_type, '-')
        status_text = ('✓ Tamamlandı (tüm veri çekildi)' if self.finished
                       else '⚠ Max iterasyon sınırı — tekrar çalıştırın')
        status_color = '#059669' if self.finished else '#d97706'

        return f"""\
<div style="font-family:Inter,'Segoe UI',system-ui,sans-serif;color:#1e293b;line-height:1.5">
<style>
.sr-header {{ background:linear-gradient(135deg,#0f172a 0%,#0369a1 50%,#0891b2 100%);
    color:#fff;padding:28px 32px;border-radius:16px;margin-bottom:20px; }}
.sr-header h2 {{ margin:0 0 4px;font-size:1.35em;font-weight:700; }}
.sr-header .sr-sub {{ font-size:0.85em;opacity:0.75; }}
.sr-cards {{ display:flex;gap:14px;margin-bottom:16px; }}
.sr-card {{ flex:1;background:#fff;border-radius:12px;padding:20px;
    box-shadow:0 1px 3px rgba(0,0,0,0.07);border-top:4px solid;text-align:center; }}
.sr-card-label {{ font-size:0.7em;font-weight:700;text-transform:uppercase;
    letter-spacing:0.08em;color:#64748b;margin-bottom:10px; }}
.sr-card-num {{ font-size:2.2em;font-weight:800;line-height:1;margin-bottom:6px; }}
.sr-card-sub {{ font-size:0.8em;color:#94a3b8; }}
.c-fetch {{ border-color:#64748b; }} .c-fetch .sr-card-num {{ color:#475569; }}
.c-new   {{ border-color:#10b981; }} .c-new   .sr-card-num {{ color:#059669; }}
.c-upd   {{ border-color:#3b82f6; }} .c-upd   .sr-card-num {{ color:#2563eb; }}
.c-del   {{ border-color:#ef4444; }} .c-del   .sr-card-num {{ color:#dc2626; }}
</style>

<div class="sr-header">
    <h2>GİB Mükellef Senkronizasyon Raporu</h2>
    <div class="sr-sub">Mod: {mode_label} &middot; Doküman: {doc_label} &middot;
        İterasyon: {self.total_iterations}</div>
    <div style="margin-top:8px;padding:6px 12px;background:rgba(255,255,255,0.15);
         border-radius:8px;display:inline-block;color:{status_color};
         background:#fff;font-weight:600;font-size:0.85em">
         {status_text}
    </div>
</div>

<div class="sr-cards">
    <div class="sr-card c-fetch">
        <div class="sr-card-label">Çekilen</div>
        <div class="sr-card-num">{fmt(self.total_fetched)}</div>
        <div class="sr-card-sub">kayıt</div>
    </div>
    <div class="sr-card c-new">
        <div class="sr-card-label">Yeni Eklenen</div>
        <div class="sr-card-num">{fmt(self.total_created)}</div>
        <div class="sr-card-sub">kayıt</div>
    </div>
    <div class="sr-card c-upd">
        <div class="sr-card-label">Güncellenen</div>
        <div class="sr-card-num">{fmt(self.total_updated)}</div>
        <div class="sr-card-sub">kayıt</div>
    </div>
    <div class="sr-card c-del">
        <div class="sr-card-label">Silinmiş İşaretli</div>
        <div class="sr-card-num">{fmt(self.total_deleted_mark)}</div>
        <div class="sr-card-sub">kayıt</div>
    </div>
</div>
</div>"""
