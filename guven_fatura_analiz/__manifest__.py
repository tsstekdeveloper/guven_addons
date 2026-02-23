{
    'name': 'Güven Hastanesi Fatura Analiz Uygulaması',
    'version': '19.0.1.0.0',
    'category': 'Accounting',
    'summary': 'E-Fatura analiz ve takip modülü',
    'description': """
        Güven Hastanesi için E-Fatura/E-Arşiv analiz modülü.
        - izibiz SOAP entegrasyonu
        - Logo MSSQL entegrasyonu
        - Çok şirketli yapı desteği
    """,
    'external_dependencies': {
        'python': ['pymssql', 'zeep'],
    },
    'author': 'Güven Hastanesi',
    'depends': ['base', 'account', 'mail'],
    'data': [
        'security/security_groups.xml',
        'security/ir.model.access.csv',
        'security/security_rules.xml',
        'views/res_company_views.xml',
        'views/guven_fatura_views.xml',
        'views/guven_fatura_sync_wizard_views.xml',
        'views/guven_logo_fatura_views.xml',
        'views/guven_logo_sync_wizard_views.xml',
        'views/menus.xml',
        'data/cron_data.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'guven_fatura_analiz/static/src/css/logo_eslestirme.scss',
        ],
    },
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
