{
    'name': 'Güven HBYS Sistemleri Karşılaştırması',
    'version': '19.0.1.0.3',
    'category': 'Accounting',
    'summary': 'İzibiz faturalarını Pusula ve Cerebrum kayıtlarıyla karşılaştırır',
    'description': """
        Geçerli izibiz faturalarını şirket bazında Pusula (MSSQL) ve
        Cerebrum (Oracle) kayıtlarında arar. Kaynak veriyi kopyalamaz,
        yalnızca eşleşme sonucunu ve dayandığı alanların anlık görüntüsünü saklar.
        Tasarım: docs-reports/kaynak_karsilastirma_tasarim.md
    """,
    'author': 'Güven Hastanesi',
    'depends': ['guven_fatura_analiz'],
    'external_dependencies': {
        'python': ['pymssql', 'oracledb'],
    },
    'data': [
        'security/ir.model.access.csv',
        'security/security_rules.xml',
        'data/source_data.xml',
        'data/cron_data.xml',
        'views/kaynak_karsilastirma_views.xml',
        'views/kaynak_source_views.xml',
        'views/res_company_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
