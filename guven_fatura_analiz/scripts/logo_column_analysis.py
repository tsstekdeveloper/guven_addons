"""Logo MSSQL LG_600_01_INVOICE tablo kolon analizi."""
import json

Fatura = env['guven.fatura'].sudo()
Company = env['res.company'].sudo()

company = Company.browse(2)  # ANKARA GÜVEN HASTANESİ
creds = company.get_logo_credentials()

import pymssql
conn = pymssql.connect(
    server=creds['server'],
    port=creds['port'],
    user=creds['username'],
    password=creds['password'],
    database=creds['database'],
    timeout=30, login_timeout=30, charset='cp1254',
)
cursor = conn.cursor()

table = creds['invoice_table']  # LG_600_01_INVOICE

# 1. Kolon bilgilerini çek
print("=== KOLON BİLGİLERİ ===")
cursor.execute("""
    SELECT c.COLUMN_NAME, c.DATA_TYPE, c.CHARACTER_MAXIMUM_LENGTH,
           c.NUMERIC_PRECISION, c.NUMERIC_SCALE, c.IS_NULLABLE,
           c.COLUMN_DEFAULT, c.ORDINAL_POSITION
    FROM INFORMATION_SCHEMA.COLUMNS c
    WHERE c.TABLE_NAME = %s
    ORDER BY c.ORDINAL_POSITION
""", (table,))
columns = cursor.fetchall()
col_names = [r[0] for r in columns]

print(f"Toplam kolon: {len(columns)}")
print("---")
for col in columns:
    name, dtype, max_len, num_prec, num_scale, nullable, default, pos = col
    size_info = ""
    if max_len:
        size_info = f"({max_len})"
    elif num_prec:
        size_info = f"({num_prec},{num_scale})" if num_scale else f"({num_prec})"
    print(f"{pos:3d}. {name:40s} {dtype}{size_info:15s} null={nullable} default={default}")

# 2. Toplam kayıt sayısı ve TRCODE dağılımı
print("\n=== TRCODE DAĞILIMI ===")
cursor.execute(f"SELECT TRCODE, COUNT(*) as cnt FROM {table} GROUP BY TRCODE ORDER BY TRCODE")
for row in cursor.fetchall():
    print(f"  TRCODE={row[0]}: {row[1]} kayıt")

# 3. CANCELLED dağılımı
print("\n=== CANCELLED DAĞILIMI ===")
cursor.execute(f"SELECT CANCELLED, COUNT(*) FROM {table} GROUP BY CANCELLED")
for row in cursor.fetchall():
    print(f"  CANCELLED={row[0]}: {row[1]}")

# 4. Bizdeki birkaç faturayı Logo'da bul ve kolon değerlerini karşılaştır
print("\n=== ÖRNEK FATURA EŞLEŞTİRME ===")

# Giden faturalar (OUT) - Ankara Güven
sample_out = Fatura.search([
    ('company_id', '=', 2),
    ('kaynak', '=', 'e-fatura-izibiz'),
    ('direction', '=', 'OUT'),
    ('issue_date', '!=', False),
    ('details_received', '=', True),
], limit=5, order='issue_date desc')

# Gelen faturalar (IN) - Ankara Güven
sample_in = Fatura.search([
    ('company_id', '=', 2),
    ('kaynak', '=', 'e-fatura-izibiz'),
    ('direction', '=', 'IN'),
    ('issue_date', '!=', False),
    ('details_received', '=', True),
], limit=5, order='issue_date desc')

for label, sample, trcode_cond in [
    ("OUT (giden)", sample_out, "TRCODE IN (6,7,8,9,14)"),
    ("IN (gelen)", sample_in, "TRCODE IN (1,3,4,13)"),
]:
    print(f"\n--- {label} ---")
    for inv in sample:
        inv_id = inv.invoice_id
        inv_date = inv.issue_date.strftime('%Y-%m-%d') if inv.issue_date else None
        if not inv_date:
            continue

        query = f"""
            SELECT TOP 1 LOGICALREF, FICHENO, DOCODE, DATE_, TRCODE,
                   CLIENTREF, CANCELLED, TOTALDISCOUNTED,
                   TOTALVAT, GROSSTOTAL, NETTOTAL, REPORTRATE, REPORTNET,
                   TRCURR, TRRATE, TRNET, GUID, PROFILEID,
                   EINVOICE, EINVOICETYP, ESTATUS, TYPECODE,
                   TOTALADDTAX, TOTALEXADDTAX, GENEXP1, GENEXP2
            FROM {table}
            WHERE (FICHENO = %s OR DOCODE = %s)
            AND CANCELLED = 0
            AND CAST(DATE_ AS DATE) = %s
            AND {trcode_cond}
        """
        cursor.execute(query, (inv_id, inv_id, inv_date))
        result = cursor.fetchone()

        if result:
            print(f"\n  Odoo: {inv_id} | tarih={inv_date} | tutar={inv.payable_amount:.2f} | "
                  f"vergisiz={inv.tax_exclusive_amount:.2f} | kur={inv.currency_code}")
            print(f"  Logo: LOGICALREF={result[0]} FICHENO={result[1]} DOCODE={result[2]} "
                  f"DATE_={result[3]} TRCODE={result[4]}")
            print(f"        CLIENTREF={result[5]} CANCELLED={result[6]}")
            print(f"        TOTALDISCOUNTED={result[7]} TOTALVAT={result[8]} "
                  f"GROSSTOTAL={result[9]} NETTOTAL={result[10]}")
            print(f"        REPORTRATE={result[11]} REPORTNET={result[12]}")
            print(f"        TRCURR={result[13]} TRRATE={result[14]} TRNET={result[15]}")
            print(f"        GUID={result[16]} PROFILEID={result[17]} EINVOICE={result[18]}")
            print(f"        EINVOICETYP={result[19]} ESTATUS={result[20]} TYPECODE={result[21]}")
            print(f"        TOTALADDTAX={result[22]} TOTALEXADDTAX={result[23]}")
            print(f"        GENEXP1={result[24]} GENEXP2={result[25]}")
        else:
            print(f"\n  Odoo: {inv_id} | tarih={inv_date} -> Logo'da BULUNAMADI")

# 5. Bir Logo kaydının TÜM kolon değerlerini göster (eşleşen ilk giden fatura)
print("\n\n=== TAM KOLON DUMP (ilk eşleşen OUT fatura) ===")
if sample_out:
    inv = sample_out[0]
    inv_id = inv.invoice_id
    inv_date = inv.issue_date.strftime('%Y-%m-%d')
    col_list = ', '.join(col_names)
    query = f"""
        SELECT TOP 1 {col_list}
        FROM {table}
        WHERE (FICHENO = %s OR DOCODE = %s)
        AND CANCELLED = 0
        AND CAST(DATE_ AS DATE) = %s
        AND TRCODE IN (6,7,8,9,14)
    """
    cursor.execute(query, (inv_id, inv_id, inv_date))
    full_row = cursor.fetchone()
    if full_row:
        print(f"Fatura: {inv_id} (Odoo payable={inv.payable_amount:.2f} currency={inv.currency_code})")
        for cname, val in zip(col_names, full_row):
            if val is not None and val != 0 and val != '' and val != 0.0:
                print(f"  {cname:40s} = {val}")

# 6. Bir Logo kaydının TÜM kolon değerlerini göster (ilk eşleşen gelen fatura)
print("\n\n=== TAM KOLON DUMP (ilk eşleşen IN fatura) ===")
if sample_in:
    inv = sample_in[0]
    inv_id = inv.invoice_id
    inv_date = inv.issue_date.strftime('%Y-%m-%d')
    col_list = ', '.join(col_names)
    query = f"""
        SELECT TOP 1 {col_list}
        FROM {table}
        WHERE (FICHENO = %s OR DOCODE = %s)
        AND CANCELLED = 0
        AND CAST(DATE_ AS DATE) = %s
        AND TRCODE IN (1,3,4,13)
    """
    cursor.execute(query, (inv_id, inv_id, inv_date))
    full_row = cursor.fetchone()
    if full_row:
        print(f"Fatura: {inv_id} (Odoo payable={inv.payable_amount:.2f} currency={inv.currency_code})")
        for cname, val in zip(col_names, full_row):
            if val is not None and val != 0 and val != '' and val != 0.0:
                print(f"  {cname:40s} = {val}")

conn.close()
print("\n=== BİTTİ ===")
