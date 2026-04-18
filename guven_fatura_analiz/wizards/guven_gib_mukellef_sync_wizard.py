import json
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
    max_iterations_per_company = fields.Integer(
        string='Şirket Başına Max İterasyon',
        default=10,
        help='Her şirket için izibiz\'e yapılacak max çağrı sayısı '
             '(her çağrı ~100 kayıt). Çalışma timeout olursa tekrar '
             'tetikleyin; her şirket kaldığı yerden devam eder.',
    )
    restart_from_beginning = fields.Boolean(
        string='Baştan Başla',
        default=False,
        help='Tamamı modunda işaretlenirse, tüm şirketlerin saklı cursor\'ı '
             'sıfırlanır ve 2013-01-01\'den başlanır.',
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
    total_iterations = fields.Integer(string='Toplam İterasyon', readonly=True)
    total_companies = fields.Integer(string='İşlenen Şirket', readonly=True)
    all_finished = fields.Boolean(
        string='Tüm Şirketler Tamamlandı',
        readonly=True,
    )
    company_results_json = fields.Text(readonly=True)
    log_messages = fields.Text(string='İşlem Logları', readonly=True)
    report_html = fields.Html(
        string='Rapor',
        compute='_compute_report_html',
        sanitize=False,
    )

    # Config parameter key prefix: her şirket için ayrı cursor saklanır
    _CURSOR_KEY_PREFIX = 'guven_fatura_analiz.gib_mukellef_full_cursor_company_'

    # ── Computed ─────────────────────────────────────────────────

    @api.depends_context('uid')
    def _compute_last_sync_date(self):
        self.env.cr.execute(
            "SELECT MAX(last_synced_at) FROM guven_gib_mukellef"
        )
        row = self.env.cr.fetchone()
        last_date = row[0] if row and row[0] else False
        for rec in self:
            rec.last_sync_date = last_date

    @api.depends(
        'state', 'total_fetched', 'total_created', 'total_updated',
        'total_deleted_mark', 'total_iterations', 'total_companies',
        'all_finished', 'company_results_json', 'sync_mode', 'document_type',
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

        # izibiz credential'ı olan tüm şirketleri al
        all_companies = self.env['res.company'].sudo().search([])
        companies = all_companies.filtered(lambda c: c.has_efatura_credentials())
        if not companies:
            raise UserError(_(
                "izibiz E-Fatura kimlik bilgileri tanımlı şirket bulunamadı."
            ))
        log_lines.append(
            f"{len(companies)} şirket işlenecek: "
            f"{', '.join(companies.mapped('name'))}"
        )

        doc_type = self.document_type if self.document_type != 'ALL' else None
        log_lines.append(f"DOCUMENT_TYPE = {doc_type or 'ALL (hepsi)'}")
        log_lines.append(
            f"Max iterasyon/şirket: {self.max_iterations_per_company}"
        )
        log_lines.append("")

        ICPSudo = self.env['ir.config_parameter'].sudo()
        GibFatura = self.env['guven.fatura']

        # Baştan başla isteniyorsa tüm şirketlerin cursor'ını sil
        if self.sync_mode == 'full' and self.restart_from_beginning:
            for company in companies:
                ICPSudo.set_param(
                    f"{self._CURSOR_KEY_PREFIX}{company.id}", False,
                )
            log_lines.append("Tüm şirketlerin cursor'ı sıfırlandı.")
            log_lines.append("")

        grand_totals = {
            'fetched': 0, 'created': 0, 'updated': 0, 'deleted_mark': 0,
        }
        grand_iterations = 0
        all_finished = True
        company_results = []

        # Şirket döngüsü
        for company in companies:
            log_lines.append(f"=== {company.name} ===")
            try:
                result = self._sync_one_company(
                    company, doc_type, log_lines, ICPSudo, GibFatura,
                )
            except Exception as e:
                _logger.exception(
                    "[GUVEN-MUKELLEF] %s sync hatası", company.name,
                )
                log_lines.append(f"  HATA: {e}")
                company_results.append({
                    'name': company.name,
                    'error': str(e),
                    'fetched': 0, 'created': 0, 'updated': 0,
                    'deleted_mark': 0, 'iterations': 0, 'finished': False,
                })
                all_finished = False
                continue

            for k in grand_totals:
                grand_totals[k] += result[k]
            grand_iterations += result['iterations']
            if not result['finished']:
                all_finished = False
            company_results.append({'name': company.name, **result})
            log_lines.append("")

        log_lines.append(
            f"GENEL TOPLAM: {grand_totals['fetched']} çekildi, "
            f"{grand_totals['created']} yeni, {grand_totals['updated']} günc., "
            f"{grand_iterations} iterasyon, {len(companies)} şirket"
        )
        if all_finished:
            log_lines.append(
                "✓ Tüm şirketler için sync tamamlandı (tüm veri çekildi)"
            )
        else:
            log_lines.append(
                "⚠ Bazı şirketler için max iterasyon sınırına ulaşıldı. "
                "Tekrar çalıştırın — her şirket kaldığı yerden devam eder."
            )

        self.write({
            'state': 'done',
            'total_fetched': grand_totals['fetched'],
            'total_created': grand_totals['created'],
            'total_updated': grand_totals['updated'],
            'total_deleted_mark': grand_totals['deleted_mark'],
            'total_iterations': grand_iterations,
            'total_companies': len(companies),
            'all_finished': all_finished,
            'company_results_json': json.dumps(
                company_results, ensure_ascii=False,
            ),
            'log_messages': '\n'.join(log_lines),
        })

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

    # ── Per-Company Sync ──────────────────────────────────────────

    def _sync_one_company(self, company, doc_type, log_lines, ICPSudo, GibFatura):
        """Tek bir şirket için cursor-based iterative sync."""
        cursor_key = f"{self._CURSOR_KEY_PREFIX}{company.id}"

        # Başlangıç cursor'ı
        if self.sync_mode == 'full':
            cursor_str = ICPSudo.get_param(cursor_key)
            if cursor_str:
                try:
                    cursor = fields.Datetime.from_string(cursor_str)
                except Exception:
                    cursor = datetime(2013, 1, 1)
            else:
                cursor = datetime(2013, 1, 1)
        else:
            cursor = self.last_sync_date or (
                fields.Datetime.now() - timedelta(days=7)
            )
        log_lines.append(
            f"  cursor başlangıcı: {cursor.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        # SOAP login
        client, session_id, request_header = \
            GibFatura._get_soap_client_and_login(company)
        client.transport.load_timeout = 300
        client.transport.operation_timeout = 300

        totals = {'fetched': 0, 'created': 0, 'updated': 0, 'deleted_mark': 0}
        iteration = 0
        finished = False
        last_cursor = cursor

        try:
            while iteration < self.max_iterations_per_company:
                iteration += 1
                soap_args = {
                    'REQUEST_HEADER': request_header,
                    'REGISTER_TIME_START': cursor,
                }
                if doc_type:
                    soap_args['DOCUMENT_TYPE'] = doc_type

                _logger.info(
                    "[GUVEN-MUKELLEF] %s: İt %s/%s, cursor=%s",
                    company.name, iteration,
                    self.max_iterations_per_company,
                    cursor.strftime('%Y-%m-%d %H:%M:%S'),
                )
                with client.settings(raw_response=True):
                    raw = client.service.GetUserList(**soap_args)

                user_records = self._parse_users(raw.content)
                if not user_records:
                    finished = True
                    log_lines.append(f"  [İt-{iteration}] Boş cevap, bittik.")
                    break

                max_reg = max(
                    (r['register_time'] for r in user_records
                     if r.get('register_time')),
                    default=None,
                )

                stats = self._upsert_records(user_records)
                for k in totals:
                    totals[k] += stats[k]

                log_lines.append(
                    f"  [İt-{iteration}] {stats['fetched']} kayıt → "
                    f"{stats['created']} yeni, {stats['updated']} günc., "
                    f"cursor→{max_reg.strftime('%Y-%m-%d %H:%M:%S') if max_reg else '?'}"
                )

                # 100'den az geldiyse son sayfa
                if len(user_records) < 100:
                    finished = True
                    break

                if max_reg is None:
                    finished = True
                    log_lines.append(
                        "  [Uyarı] Kayıtlarda register_time yok, duruldu."
                    )
                    break

                new_cursor = max_reg + timedelta(seconds=1)
                if new_cursor <= last_cursor:
                    log_lines.append(
                        "  [Uyarı] Cursor ilerlemiyor, döngü kırılıyor."
                    )
                    break
                cursor = new_cursor
                last_cursor = cursor

                # Full modda checkpoint
                if self.sync_mode == 'full':
                    ICPSudo.set_param(
                        cursor_key, fields.Datetime.to_string(cursor),
                    )
                self.env.cr.commit()

            # Full modda bitim durumunu yansıt
            if self.sync_mode == 'full':
                if finished:
                    ICPSudo.set_param(cursor_key, False)
                else:
                    ICPSudo.set_param(
                        cursor_key, fields.Datetime.to_string(cursor),
                    )
        finally:
            try:
                client.service.Logout(REQUEST_HEADER=request_header)
            except Exception:
                pass

        status = "✓ tamamlandı" if finished else "⚠ max iterasyon"
        log_lines.append(
            f"  Özet: {totals['fetched']} çekildi, {totals['created']} yeni, "
            f"{totals['updated']} günc. ({status})"
        )

        return {**totals, 'iterations': iteration, 'finished': finished}

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
        status_text = ('✓ Tüm şirketler tamamlandı' if self.all_finished
                       else '⚠ Bazı şirketlerde max iterasyon — tekrar çalıştırın')
        status_color = '#059669' if self.all_finished else '#d97706'

        # Şirket satırları
        company_rows = ''
        try:
            companies = json.loads(self.company_results_json or '[]')
        except Exception:
            companies = []
        for c in companies:
            if c.get('error'):
                company_rows += (
                    '<tr>'
                    f'<td style="text-align:left;font-weight:600">{c["name"]}</td>'
                    f'<td colspan="5" style="color:#ef4444;text-align:center">'
                    f'Hata: {c["error"][:80]}</td>'
                    '</tr>'
                )
                continue
            bitti = '✓' if c.get('finished') else '⚠'
            company_rows += (
                '<tr>'
                f'<td style="text-align:left;font-weight:600">{c["name"]}</td>'
                f'<td>{fmt(c["fetched"])}</td>'
                f'<td><strong>{fmt(c["created"])}</strong></td>'
                f'<td>{fmt(c["updated"])}</td>'
                f'<td>{c["iterations"]}</td>'
                f'<td>{bitti}</td>'
                '</tr>'
            )

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
.c-it    {{ border-color:#a855f7; }} .c-it    .sr-card-num {{ color:#7c3aed; }}
.sr-table {{ width:100%;border-collapse:collapse;margin-top:12px;font-size:0.9em;
    border-radius:12px;overflow:hidden;border:1px solid #e2e8f0; }}
.sr-table th {{ background:#f1f5f9;padding:10px 14px;text-align:center;
    font-size:0.75em;text-transform:uppercase;color:#64748b; }}
.sr-table td {{ padding:10px 14px;text-align:center;border-bottom:1px solid #f1f5f9; }}
.sr-table tbody tr:last-child td {{ border-bottom:none; }}
</style>

<div class="sr-header">
    <h2>GİB Mükellef Senkronizasyon Raporu</h2>
    <div class="sr-sub">Mod: {mode_label} &middot; Doküman: {doc_label} &middot;
        {self.total_companies} şirket &middot; {self.total_iterations} iterasyon</div>
    <div style="margin-top:8px;padding:6px 12px;
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
    <div class="sr-card c-it">
        <div class="sr-card-label">Şirket</div>
        <div class="sr-card-num">{self.total_companies}</div>
        <div class="sr-card-sub">işlendi</div>
    </div>
</div>

<table class="sr-table">
    <thead>
        <tr>
            <th style="text-align:left">Şirket</th>
            <th>Çekilen</th>
            <th>Yeni</th>
            <th>Güncellenen</th>
            <th>İterasyon</th>
            <th>Durum</th>
        </tr>
    </thead>
    <tbody>
        {company_rows}
    </tbody>
</table>
</div>"""
