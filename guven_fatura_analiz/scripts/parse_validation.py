#!/usr/bin/env python3
"""
Parse Doğrulama Scripti
========================
Her şirketten rastgele e-fatura ve e-arşiv faturaları çekip:
1. Raw SOAP XML verilerini DB kayıtlarıyla karşılaştırır
2. TaxScheme parse doğruluğunu kontrol eder
3. Çoklu şirket veri izolasyonunu test eder
4. Sonuçları Markdown raporu olarak kaydeder

Kullanım: Odoo shell içinden çalıştırılır.
"""

import base64
import io
import logging
import random
import re
import time
import zipfile
from collections import defaultdict
from datetime import datetime
from xml.etree import ElementTree as ET

_logger = logging.getLogger(__name__)

REPORT_PATH = '/mnt/extra-addons/guven_fatura_analiz/scripts/_validation_report.md'


def run(env):
    """Ana doğrulama fonksiyonu."""
    report_lines = []
    rpt = report_lines.append

    rpt("# E-Fatura Parse Doğrulama Raporu")
    rpt(f"\n**Tarih:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    rpt(f"**Odoo Sürümü:** 19.0")
    rpt("")

    Fatura = env['guven.fatura'].sudo()
    Tax = env['guven.fatura.tax'].sudo()
    Company = env['res.company'].sudo()

    companies = Company.search([])
    rpt(f"**Toplam Şirket:** {len(companies)}")
    rpt("")

    # ============================================================
    # BÖLÜM 1: Şirket bazında SOAP vs DB karşılaştırması
    # ============================================================
    rpt("---")
    rpt("## 1. SOAP XML vs DB Karşılaştırması")
    rpt("")

    all_issues = []
    all_tax_findings = []
    company_fetch_results = {}  # company_id -> fetched invoice data for isolation check

    for company in companies:
        rpt(f"### Şirket: {company.name} (ID: {company.id})")
        rpt("")

        # E-fatura ve e-arşiv faturalarını say
        efatura_count = Fatura.search_count([
            ('company_id', '=', company.id),
            ('kaynak', '=', 'e-fatura-izibiz'),
            ('details_received', '=', True),
        ])
        earsiv_count = Fatura.search_count([
            ('company_id', '=', company.id),
            ('kaynak', '=', 'e-arsiv-izibiz'),
            ('details_received', '=', True),
        ])

        rpt(f"- Detayı çekilmiş e-fatura: **{efatura_count}**")
        rpt(f"- Detayı çekilmiş e-arşiv: **{earsiv_count}**")
        rpt("")

        if efatura_count == 0 and earsiv_count == 0:
            rpt("> Bu şirket için detayı çekilmiş fatura yok, atlanıyor.")
            rpt("")
            continue

        # Credential kontrolü
        try:
            if not company.has_efatura_credentials():
                rpt("> SOAP kimlik bilgileri tanımsız, atlanıyor.")
                rpt("")
                continue
        except Exception as e:
            rpt(f"> Credential kontrol hatası: {e}")
            rpt("")
            continue

        # Her kaynak türünden rastgele fatura seç
        fetched_invoices = []

        for kaynak in ('e-fatura-izibiz', 'e-arsiv-izibiz'):
            pool = Fatura.search([
                ('company_id', '=', company.id),
                ('kaynak', '=', kaynak),
                ('details_received', '=', True),
            ])
            if not pool:
                continue

            sample_size = min(random.randint(5, 15), len(pool))
            sample_ids = random.sample(pool.ids, sample_size)
            sample = Fatura.browse(sample_ids)

            rpt(f"#### {kaynak.upper()} - {sample_size} fatura seçildi")
            rpt("")

            # SOAP bağlantısı
            try:
                if kaynak == 'e-fatura-izibiz':
                    client, session_id, req_header = Fatura._get_soap_client_and_login(company)
                    soap_logout_client = client
                else:
                    ef_client, ea_client, session_id, req_header = Fatura._get_earsiv_soap_client(company)
                    client = ea_client
                    soap_logout_client = ef_client
            except Exception as e:
                rpt(f"> SOAP bağlantı hatası: {e}")
                rpt("")
                continue

            try:
                for inv in sample:
                    result = _validate_single_invoice(
                        env, inv, client, req_header, kaynak, company,
                    )
                    fetched_invoices.append(result)
                    all_issues.extend(result.get('issues', []))
                    all_tax_findings.extend(result.get('tax_findings', []))
            finally:
                try:
                    soap_logout_client.service.Logout(REQUEST_HEADER=req_header)
                except Exception:
                    pass

        company_fetch_results[company.id] = fetched_invoices

        # Şirket sonuçları tablosu
        if fetched_invoices:
            rpt("| Fatura No | Kaynak | Durum | Header Eşleşme | Vergi Eşleşme | Sorunlar |")
            rpt("|-----------|--------|-------|----------------|---------------|----------|")
            for r in fetched_invoices:
                status_icon = "OK" if not r['issues'] else "SORUN"
                header_match = r.get('header_match', '-')
                tax_match = r.get('tax_match', '-')
                issues_str = "; ".join(r['issues'][:3]) if r['issues'] else "-"
                rpt(f"| {r['invoice_id']} | {r['kaynak']} | {status_icon} | {header_match} | {tax_match} | {issues_str} |")
            rpt("")

    # ============================================================
    # BÖLÜM 2: TaxScheme Parse Analizi
    # ============================================================
    rpt("---")
    rpt("## 2. TaxScheme Parse Analizi")
    rpt("")

    if all_tax_findings:
        # Eşleşen / eşleşmeyen grupla
        matched = [t for t in all_tax_findings if t['status'] == 'matched']
        mismatched = [t for t in all_tax_findings if t['status'] == 'mismatch']
        unknown = [t for t in all_tax_findings if t['status'] == 'unknown']

        rpt(f"- Toplam vergi kaydı incelendi: **{len(all_tax_findings)}**")
        rpt(f"- Doğru eşleşen: **{len(matched)}**")
        rpt(f"- Uyumsuz (DB != XML): **{len(mismatched)}**")
        rpt(f"- Bilinmeyen TaxScheme: **{len(unknown)}**")
        rpt("")

        if mismatched:
            rpt("### Uyumsuz Vergi Kayıtları")
            rpt("")
            rpt("| Fatura | Scheme ID | Scheme Name | DB tax_type | Beklenen | Tutar |")
            rpt("|--------|-----------|-------------|-------------|----------|-------|")
            for t in mismatched:
                rpt(f"| {t['invoice_id']} | {t['scheme_id']} | {t['scheme_name']} | {t['db_type']} | {t['expected_type']} | {t['amount']:.2f} |")
            rpt("")

        if unknown:
            rpt("### Hâlâ Bilinmeyen TaxScheme'ler")
            rpt("")
            rpt("| Fatura | Scheme ID | Scheme Name | Atanan Tür |")
            rpt("|--------|-----------|-------------|------------|")
            for t in unknown:
                rpt(f"| {t['invoice_id']} | {t['scheme_id']} | {t['scheme_name']} | {t.get('assigned_type', 'diger')} |")
            rpt("")

        # tax_type dağılımı (XML'den)
        type_dist = defaultdict(int)
        for t in all_tax_findings:
            type_dist[t.get('expected_type') or t.get('assigned_type') or 'N/A'] += 1
        rpt("### Vergi Tipi Dağılımı (XML'den)")
        rpt("")
        rpt("| Vergi Tipi | Adet |")
        rpt("|------------|------|")
        for tt, cnt in sorted(type_dist.items(), key=lambda x: -x[1]):
            rpt(f"| {tt} | {cnt} |")
        rpt("")
    else:
        rpt("> Herhangi bir vergi kaydı incelenemedi.")
        rpt("")

    # ============================================================
    # BÖLÜM 3: DB'deki Genel Tax Dağılımı
    # ============================================================
    rpt("---")
    rpt("## 3. DB'deki Vergi Tipi Dağılımı")
    rpt("")
    env.cr.execute("""
        SELECT tax_type, COUNT(*) as cnt
        FROM guven_fatura_tax
        GROUP BY tax_type
        ORDER BY cnt DESC
    """)
    rpt("| tax_type | Adet |")
    rpt("|----------|------|")
    for row in env.cr.fetchall():
        rpt(f"| {row[0] or 'NULL'} | {row[1]} |")
    rpt("")

    # NULL tax_type kalan kayıtlar
    env.cr.execute("""
        SELECT t.id, t.fatura_id, f.invoice_id, t.tax_amount, t.percent
        FROM guven_fatura_tax t
        JOIN guven_fatura f ON t.fatura_id = f.id
        WHERE t.tax_type IS NULL
        LIMIT 20
    """)
    null_rows = env.cr.fetchall()
    if null_rows:
        rpt("### NULL tax_type Kalan Kayıtlar")
        rpt("")
        rpt("| Tax ID | Fatura | Tutar | Oran |")
        rpt("|--------|--------|-------|------|")
        for row in null_rows:
            rpt(f"| {row[0]} | {row[2]} | {row[3]:.2f} | {row[4]:.2f}% |")
        rpt("")
    else:
        rpt("> NULL tax_type kalan kayıt yok.")
        rpt("")

    # ============================================================
    # BÖLÜM 4: Çoklu Şirket Veri İzolasyonu
    # ============================================================
    rpt("---")
    rpt("## 4. Çoklu Şirket Veri İzolasyonu Testi")
    rpt("")

    isolation_issues = _check_company_isolation(env, rpt)

    # ============================================================
    # BÖLÜM 5: Özet
    # ============================================================
    rpt("---")
    rpt("## 5. Genel Özet")
    rpt("")

    total_checked = sum(len(v) for v in company_fetch_results.values())
    total_issues = len(all_issues)
    rpt(f"- Toplam incelenen fatura: **{total_checked}**")
    rpt(f"- Toplam tespit edilen sorun: **{total_issues}**")
    rpt(f"- İzolasyon ihlali: **{len(isolation_issues)}**")
    rpt("")

    if total_issues == 0 and len(isolation_issues) == 0:
        rpt("> **SONUÇ: Tüm kontroller başarılı.**")
    else:
        if total_issues > 0:
            rpt(f"> **UYARI: {total_issues} parse/eşleşme sorunu tespit edildi.**")
        if isolation_issues:
            rpt(f"> **KRİTİK: {len(isolation_issues)} izolasyon ihlali tespit edildi!**")
    rpt("")

    # Raporu dosyaya yaz
    report_text = "\n".join(report_lines)

    # Container içindeki geçici dosyaya yaz
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write(report_text)

    _logger.info("[VALIDATION] Rapor yazıldı: %s", REPORT_PATH)
    print("\n" + "=" * 60)
    print("RAPOR TAMAMLANDI")
    print("=" * 60)
    print(f"Toplam fatura: {total_checked}")
    print(f"Sorunlar: {total_issues}")
    print(f"İzolasyon ihlali: {len(isolation_issues)}")
    print("=" * 60)

    return report_text


def _validate_single_invoice(env, inv, client, req_header, kaynak, company):
    """Tek bir faturayı SOAP'tan çekip DB ile karşılaştır."""
    result = {
        'invoice_id': inv.invoice_id,
        'uuid': inv.uuid,
        'kaynak': kaynak,
        'company_id': company.id,
        'company_name': company.name,
        'issues': [],
        'tax_findings': [],
        'header_match': '-',
        'tax_match': '-',
    }

    try:
        ubl_bytes = _fetch_raw_xml(inv, client, req_header, kaynak)
    except Exception as e:
        result['issues'].append(f"XML çekme hatası: {e}")
        return result

    if not ubl_bytes:
        result['issues'].append("XML içeriği boş")
        return result

    try:
        _compare_header_fields(inv, ubl_bytes, result)
        _compare_tax_fields(env, inv, ubl_bytes, result)
    except Exception as e:
        result['issues'].append(f"Karşılaştırma hatası: {e}")

    return result


def _fetch_raw_xml(inv, client, req_header, kaynak):
    """SOAP'tan raw UBL XML'i çek."""
    if kaynak == 'e-fatura-izibiz':
        search_key = {
            'LIMIT': 1,
            'UUID': inv.uuid,
            'DIRECTION': inv.direction or 'IN',
            'READ_INCLUDED': 'true',
        }
        with client.settings(raw_response=True):
            raw = client.service.GetInvoice(
                REQUEST_HEADER=req_header,
                INVOICE_SEARCH_KEY=search_key,
                HEADER_ONLY='N',
            )
    else:
        with client.settings(raw_response=True):
            raw = client.service.ReadFromArchive(
                REQUEST_HEADER=req_header,
                INVOICEID=inv.uuid,
                PORTAL_DIRECTION='OUT',
                PROFILE='XML',
            )

    root = ET.fromstring(raw.content)

    # CONTENT elementini bul
    content_text = None
    max_len = 0
    for tag in ('CONTENT', 'INVOICE', 'HTML_CONTENT', 'INVOICE_CONTENT', 'DATA'):
        for elem in root.iter():
            t = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
            if t == tag and elem.text and len(elem.text.strip()) > max_len:
                content_text = elem.text.strip()
                max_len = len(content_text)

    if not content_text or max_len < 100:
        return None

    decoded = base64.b64decode(content_text)

    if decoded[:4] == b'PK\x03\x04':
        with zipfile.ZipFile(io.BytesIO(decoded), 'r') as zf:
            xml_name = next(
                (n for n in zf.namelist()
                 if n.endswith('.xml') and not n.startswith('__')),
                zf.namelist()[0] if zf.namelist() else None,
            )
            if not xml_name:
                return None
            ubl_bytes = zf.read(xml_name)
    else:
        ubl_bytes = decoded

    return ubl_bytes.replace(b'\x00', b'')


def _parse_float(value):
    """Finansal string → float."""
    if not value:
        return 0.0
    s = str(value).strip()
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    s = s.replace('₺', '').replace('TL', '').replace('$', '').strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _find_elem(parent, local_name):
    if parent is None:
        return None
    for child in parent.iter():
        tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if tag == local_name:
            return child
    return None


def _find_all_direct(parent, local_name):
    result = []
    if parent is None:
        return result
    for child in parent:
        tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if tag == local_name:
            result.append(child)
    return result


def _get_text(elem, local_name):
    found = _find_elem(elem, local_name)
    if found is not None and found.text:
        return found.text.strip()
    return ''


# TaxScheme mapping (aynı modeldeki gibi)
SCHEME_ID_MAP = {
    '0015': 'kdv',
    '0003': 'kdv',
    '9015': 'withholding',
    '4071': 'bsmv',
    '0059': 'konaklama',
    '0071': 'tuketim',
}

SCHEME_NAME_MAP = {
    'KDV': 'kdv',
    'KDV HESAPLANAN': 'kdv',
    'KATMA DEGER VERGISI': 'kdv',
    'KATMA DEĞER VERGISI': 'kdv',
    'KATMA DEĞER VERGİSİ': 'kdv',
    'GERÇEK USULDE KATMA DEĞER VERGİSİ': 'kdv',
    'KDV-SATIŞLAR': 'kdv',
    'KDV GERÇEK': 'kdv',
    'KDV GERCEK': 'kdv',
    'KDV VERGISI': 'kdv',
    'KDV VERGİSİ': 'kdv',
    'HESAPLANAN KDV': 'kdv',
    'SATIŞ VERGISI': 'kdv',
    'SATIŞ VERGİSİ': 'kdv',
    'SATIŞ KDV': 'kdv',
    'TEVKIFAT': 'withholding',
    'KDV TEVKİFAT': 'withholding',
    'BSMV': 'bsmv',
    'KONAKLAMA VERGISI': 'konaklama',
    'KONAKLAMA VERGİSİ': 'konaklama',
    'ELK.HAVAGAZ.TÜK.VER.': 'tuketim',
    'ELEKTRIK HAVAGAZI TUKETIM VERGISI': 'tuketim',
}


def _resolve_tax_type_from_xml(subtotal_elem):
    """XML'den tax_type çözümle (modeldeki resolve_tax_type'ın kopyası)."""
    scheme = _find_elem(subtotal_elem, 'TaxScheme')
    if scheme is None:
        return None, '', ''

    scheme_id = ''
    scheme_name = ''
    for child in scheme:
        child_tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if child_tag == 'ID' and child.text:
            scheme_id = child.text.strip()
        elif child_tag == 'Name' and child.text:
            scheme_name = child.text.strip().upper()

    # 1. Scheme ID ile exact match
    if scheme_id in SCHEME_ID_MAP:
        return SCHEME_ID_MAP[scheme_id], scheme_id, scheme_name
    # 2. Scheme Name ile exact match
    if scheme_name in SCHEME_NAME_MAP:
        return SCHEME_NAME_MAP[scheme_name], scheme_id, scheme_name
    # 3. Parantez kaldır
    cleaned = re.sub(r'\s*\(.*?\)', '', scheme_name).strip()
    if cleaned and cleaned in SCHEME_NAME_MAP:
        return SCHEME_NAME_MAP[cleaned], scheme_id, scheme_name
    # 4. Oran kaldır
    cleaned2 = re.sub(r'\s*%\d+', '', cleaned).strip()
    if cleaned2 and cleaned2 in SCHEME_NAME_MAP:
        return SCHEME_NAME_MAP[cleaned2], scheme_id, scheme_name
    # 5. KDV/KATMA fallback
    if 'KDV' in scheme_name or 'KATMA' in scheme_name:
        return 'kdv', scheme_id, scheme_name

    return 'diger', scheme_id, scheme_name


def _compare_header_fields(inv, ubl_bytes, result):
    """UBL XML header alanlarını DB ile karşılaştır."""
    root = ET.fromstring(ubl_bytes)
    pf = _parse_float
    issues = result['issues']
    mismatches = 0
    checks = 0

    # LegalMonetaryTotal
    legal = _find_elem(root, 'LegalMonetaryTotal')
    if legal is not None:
        field_map = {
            'TaxExclusiveAmount': ('tax_exclusive_amount', 'Vergisiz Toplam'),
            'TaxInclusiveAmount': ('tax_inclusive_amount', 'Vergili Toplam'),
            'PayableAmount': ('payable_amount', 'Ödenecek Tutar'),
            'AllowanceTotalAmount': ('allowance_total_amount', 'İndirim Toplamı'),
        }
        for xml_tag, (db_field, label) in field_map.items():
            xml_val = pf(_get_text(legal, xml_tag))
            db_val = getattr(inv, db_field, 0.0) or 0.0
            if xml_val > 0:
                checks += 1
                if abs(xml_val - db_val) > 0.01:
                    mismatches += 1
                    issues.append(
                        f"{label}: XML={xml_val:.2f} DB={db_val:.2f}"
                    )

    # Currency
    xml_currency = _get_text(root, 'DocumentCurrencyCode')
    if xml_currency and inv.currency_code:
        checks += 1
        if xml_currency != inv.currency_code:
            mismatches += 1
            issues.append(f"Para birimi: XML={xml_currency} DB={inv.currency_code}")

    # Exchange rate
    pricing_er = _find_elem(root, 'PricingExchangeRate')
    if pricing_er is not None:
        xml_rate = pf(_get_text(pricing_er, 'CalculationRate'))
        if xml_rate > 0 and inv.exchange_rate:
            checks += 1
            if abs(xml_rate - inv.exchange_rate) > 0.0001:
                mismatches += 1
                issues.append(f"Kur: XML={xml_rate:.4f} DB={inv.exchange_rate:.4f}")

    result['header_match'] = f"{checks - mismatches}/{checks}" if checks > 0 else "N/A"


def _compare_tax_fields(env, inv, ubl_bytes, result):
    """UBL XML vergi kayıtlarını DB ile karşılaştır."""
    root = ET.fromstring(ubl_bytes)
    pf = _parse_float
    issues = result['issues']
    tax_findings = result['tax_findings']

    # DB'deki vergi kayıtları
    db_taxes = env['guven.fatura.tax'].sudo().search([
        ('fatura_id', '=', inv.id),
    ])

    # XML'den vergi bilgilerini topla (tüm TaxTotal/TaxSubtotal)
    xml_taxes = []

    # Önce satır seviyesindeki vergileri topla
    invoice_lines = _find_all_direct(root, 'InvoiceLine')
    has_line_taxes = False
    for line_elem in invoice_lines:
        tax_total = _find_elem(line_elem, 'TaxTotal')
        if tax_total is not None:
            for subtotal in _find_all_direct(tax_total, 'TaxSubtotal'):
                taxable = pf(_get_text(subtotal, 'TaxableAmount'))
                tax_amt = pf(_get_text(subtotal, 'TaxAmount'))
                percent = pf(_get_text(subtotal, 'Percent'))
                resolved, sid, sname = _resolve_tax_type_from_xml(subtotal)
                xml_taxes.append({
                    'taxable': taxable,
                    'tax_amount': tax_amt,
                    'percent': percent,
                    'tax_type': resolved,
                    'scheme_id': sid,
                    'scheme_name': sname,
                    'level': 'line',
                })
                has_line_taxes = True

    # Satır seviyesinde vergi yoksa root seviyesini kullan
    if not has_line_taxes:
        for tax_total in _find_all_direct(root, 'TaxTotal'):
            for subtotal in _find_all_direct(tax_total, 'TaxSubtotal'):
                taxable = pf(_get_text(subtotal, 'TaxableAmount'))
                tax_amt = pf(_get_text(subtotal, 'TaxAmount'))
                percent = pf(_get_text(subtotal, 'Percent'))
                resolved, sid, sname = _resolve_tax_type_from_xml(subtotal)
                xml_taxes.append({
                    'taxable': taxable,
                    'tax_amount': tax_amt,
                    'percent': percent,
                    'tax_type': resolved,
                    'scheme_id': sid,
                    'scheme_name': sname,
                    'level': 'root',
                })

    # Adet karşılaştırma
    if len(xml_taxes) != len(db_taxes):
        issues.append(
            f"Vergi sayısı uyumsuz: XML={len(xml_taxes)} DB={len(db_taxes)}"
        )

    # Her XML vergi kaydını DB'de bul
    matched_count = 0
    mismatch_count = 0
    db_tax_list = list(db_taxes)

    for xt in xml_taxes:
        # DB'de en yakın eşleşmeyi bul (tutar + oran bazlı)
        best_match = None
        best_diff = float('inf')
        for dt in db_tax_list:
            diff = abs((dt.tax_amount or 0) - xt['tax_amount']) + abs((dt.percent or 0) - xt['percent'])
            if diff < best_diff:
                best_diff = diff
                best_match = dt

        finding = {
            'invoice_id': inv.invoice_id,
            'scheme_id': xt['scheme_id'],
            'scheme_name': xt['scheme_name'],
            'amount': xt['tax_amount'],
            'percent': xt['percent'],
            'expected_type': xt['tax_type'],
        }

        if best_match and best_diff < 0.1:
            db_tax_list.remove(best_match)
            finding['db_type'] = best_match.tax_type or 'NULL'

            if (best_match.tax_type or False) == (xt['tax_type'] or False):
                finding['status'] = 'matched'
                matched_count += 1
            else:
                finding['status'] = 'mismatch'
                mismatch_count += 1
                issues.append(
                    f"tax_type uyumsuz: scheme={xt['scheme_name']} "
                    f"DB={best_match.tax_type} beklenen={xt['tax_type']}"
                )

            # Tutar karşılaştırması
            if abs((best_match.tax_amount or 0) - xt['tax_amount']) > 0.01:
                issues.append(
                    f"Vergi tutarı uyumsuz ({xt['scheme_name']}): "
                    f"XML={xt['tax_amount']:.2f} DB={best_match.tax_amount:.2f}"
                )
            if abs((best_match.taxable_amount or 0) - xt['taxable']) > 0.01:
                issues.append(
                    f"Matrah uyumsuz ({xt['scheme_name']}): "
                    f"XML={xt['taxable']:.2f} DB={best_match.taxable_amount:.2f}"
                )
        else:
            finding['status'] = 'unknown'
            finding['db_type'] = 'N/A'
            finding['assigned_type'] = xt['tax_type']

        tax_findings.append(finding)

    total = matched_count + mismatch_count
    result['tax_match'] = f"{matched_count}/{total}" if total > 0 else "N/A"


def _check_company_isolation(env, rpt):
    """Çoklu şirket veri izolasyonunu kontrol et."""
    issues = []

    # Test 1: Fatura kayıtlarında şirket karışması
    rpt("### Test 1: Fatura UUID Benzersizliği (şirket bazında)")
    rpt("")
    env.cr.execute("""
        SELECT uuid, kaynak, COUNT(DISTINCT company_id) as company_count
        FROM guven_fatura
        GROUP BY uuid, kaynak
        HAVING COUNT(DISTINCT company_id) > 1
    """)
    cross_company = env.cr.fetchall()
    if cross_company:
        rpt(f"> **UYARI:** {len(cross_company)} UUID birden fazla şirkette mevcut:")
        rpt("")
        for row in cross_company[:10]:
            rpt(f"  - UUID: {row[0]}, Kaynak: {row[1]}, Şirket sayısı: {row[2]}")
        issues.extend([f"UUID çakışma: {r[0]}" for r in cross_company])
    else:
        rpt("> OK - Her UUID sadece tek bir şirkete ait.")
    rpt("")

    # Test 2: Tax kayıtlarının fatura şirketiyle tutarlılığı
    rpt("### Test 2: Vergi Kayıtları Şirket Tutarlılığı")
    rpt("")
    env.cr.execute("""
        SELECT t.id, t.fatura_id, f.company_id as fatura_company,
               t.company_id as tax_company
        FROM guven_fatura_tax t
        JOIN guven_fatura f ON t.fatura_id = f.id
        WHERE t.company_id != f.company_id
        LIMIT 10
    """)
    tax_mismatch = env.cr.fetchall()
    if tax_mismatch:
        rpt(f"> **KRİTİK:** {len(tax_mismatch)} vergi kaydında şirket uyumsuzluğu!")
        rpt("")
        for row in tax_mismatch:
            rpt(f"  - Tax ID: {row[0]}, Fatura company: {row[2]}, Tax company: {row[3]}")
        issues.extend([f"Tax şirket uyumsuz: {r[0]}" for r in tax_mismatch])
    else:
        rpt("> OK - Tüm vergi kayıtları faturalarıyla aynı şirkete ait.")
    rpt("")

    # Test 3: Line kayıtlarının fatura şirketiyle tutarlılığı
    rpt("### Test 3: Kalem Kayıtları Şirket Tutarlılığı")
    rpt("")
    env.cr.execute("""
        SELECT l.id, l.fatura_id, f.company_id as fatura_company,
               l.company_id as line_company
        FROM guven_fatura_line l
        JOIN guven_fatura f ON l.fatura_id = f.id
        WHERE l.company_id != f.company_id
        LIMIT 10
    """)
    line_mismatch = env.cr.fetchall()
    if line_mismatch:
        rpt(f"> **KRİTİK:** {len(line_mismatch)} kalem kaydında şirket uyumsuzluğu!")
        issues.extend([f"Line şirket uyumsuz: {r[0]}" for r in line_mismatch])
    else:
        rpt("> OK - Tüm kalem kayıtları faturalarıyla aynı şirkete ait.")
    rpt("")

    # Test 4: IR Rules - şirket bazlı data isolation
    rpt("### Test 4: IR Rule Varlığı")
    rpt("")
    env.cr.execute("""
        SELECT model_id, name, domain_force
        FROM ir_rule
        WHERE model_id IN (
            SELECT id FROM ir_model
            WHERE model IN ('guven.fatura', 'guven.fatura.tax',
                            'guven.fatura.line', 'guven.fatura.note')
        )
    """)
    rules = env.cr.fetchall()
    if rules:
        rpt(f"Bulunan IR Rule sayısı: **{len(rules)}**")
        rpt("")
        rpt("| Model ID | Kural Adı | Domain |")
        rpt("|----------|-----------|--------|")
        for r in rules:
            rpt(f"| {r[0]} | {r[1]} | `{r[2]}` |")
    else:
        rpt("> **UYARI:** guven.fatura modellerinde IR Rule tanımlı değil!")
        issues.append("IR Rule eksik")
    rpt("")

    # Test 5: Unique constraint kontrolü
    rpt("### Test 5: Unique Constraint Kontrolü")
    rpt("")
    env.cr.execute("""
        SELECT conname, conrelid::regclass, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = 'guven_fatura'::regclass
        AND contype = 'u'
    """)
    constraints = env.cr.fetchall()
    if constraints:
        for c in constraints:
            rpt(f"- `{c[0]}`: {c[2]}")
    else:
        rpt("> Unique constraint bulunamadı!")
        issues.append("Unique constraint eksik")
    rpt("")

    # Test 6: Şirketler arası veri sızıntısı testi (ORM seviyesi)
    rpt("### Test 6: ORM Şirket Filtreleme Testi")
    rpt("")
    Company = env['res.company'].sudo()
    Fatura = env['guven.fatura']
    companies = Company.search([])

    orm_issues = []
    for company in companies:
        # Bu şirket kullanıcısı olarak fatura sayısı
        fatura_with_company = Fatura.with_company(company).search_count([
            ('company_id', '=', company.id),
        ])
        fatura_other = Fatura.sudo().search_count([
            ('company_id', '!=', company.id),
        ])
        total_accessible = Fatura.with_company(company).search_count([])

        rpt(f"- **{company.name}**: Kendi faturaları={fatura_with_company}, "
            f"Diğer şirket faturaları={fatura_other}, "
            f"Erişebildiği toplam={total_accessible}")

        # IR rule yoksa with_company hepsini gösterebilir — bu sorun değil ama not edelim
        if total_accessible > fatura_with_company and total_accessible >= fatura_other:
            orm_issues.append(
                f"{company.name}: Diğer şirket verilerine erişebiliyor "
                f"({total_accessible} > {fatura_with_company})"
            )

    if orm_issues:
        rpt("")
        rpt("> **NOT:** IR Rule olmadığı için ORM seviyesinde tam izolasyon yok. "
            "Bu, admin kullanıcıları için beklenen davranış olabilir.")
        for oi in orm_issues:
            rpt(f">   - {oi}")
    rpt("")

    return issues


# Script Odoo shell'den doğrudan çalıştırılabilir
if __name__ == '__main__':
    # Odoo shell'den çağrılınca env global scope'ta
    pass
