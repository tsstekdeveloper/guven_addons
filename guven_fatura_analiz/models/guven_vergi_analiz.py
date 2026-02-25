from odoo import fields, models, tools


class GuvenVergiAnaliz(models.Model):
    _name = 'guven.vergi.analiz'
    _description = 'Vergi Analizi'
    _auto = False
    _order = 'month_date desc, tax_type, kategori'
    _rec_name = 'tax_type'
    _check_company_auto = True

    month_date = fields.Date('Ay', readonly=True)
    tax_type = fields.Selection(
        [
            ('kdv', 'KDV'),
            ('withholding', 'Tevkifat'),
            ('bsmv', 'BSMV'),
            ('konaklama', 'Konaklama Vergisi'),
            ('tuketim', 'Tüketim Vergisi'),
            ('oiv', 'Özel İletişim Vergisi'),
            ('damga', 'Damga Vergisi'),
            ('diger', 'Diğer'),
        ],
        string='Vergi Tipi',
        readonly=True,
    )
    kategori = fields.Selection(
        [
            ('1_gelen_gib', 'Gelen GİB'),
            ('2_gelen_logo', 'Gelen Logo Eşli'),
            ('3_giden_gib', 'Giden GİB'),
            ('4_giden_logo', 'Giden Logo Eşli'),
            ('5_gib_fark', 'GİB Fark (Giden-Gelen)'),
            ('6_logo_fark', 'Logo Eşli Fark (Giden-Gelen)'),
        ],
        string='Kategori',
        readonly=True,
    )
    company_id = fields.Many2one('res.company', string='Şirket', readonly=True)
    tutar = fields.Float('Tutar (TRY)', readonly=True)
    fatura_sayisi = fields.Integer('Fatura Sayısı', readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW %s AS (
                WITH raw_data AS (
                    SELECT
                        date_trunc('month', f.issue_date)::date AS month_date,
                        t.tax_type,
                        '1_gelen_gib' AS kategori,
                        f.company_id,
                        SUM(t.tax_amount_try) AS tutar,
                        COUNT(DISTINCT f.id) AS fatura_sayisi
                    FROM guven_fatura_tax t
                    JOIN guven_fatura f ON t.fatura_id = f.id
                    WHERE f.gvn_active = true AND f.direction = 'IN'
                    GROUP BY 1, 2, 4

                    UNION ALL

                    SELECT
                        date_trunc('month', f.issue_date)::date,
                        t.tax_type,
                        '2_gelen_logo',
                        f.company_id,
                        SUM(t.tax_amount_try),
                        COUNT(DISTINCT f.id)
                    FROM guven_fatura_tax t
                    JOIN guven_fatura f ON t.fatura_id = f.id
                    WHERE f.gvn_active = true
                        AND f.direction = 'IN'
                        AND f.logo_fatura_count = 1
                    GROUP BY 1, 2, 4

                    UNION ALL

                    SELECT
                        date_trunc('month', f.issue_date)::date,
                        t.tax_type,
                        '3_giden_gib',
                        f.company_id,
                        SUM(t.tax_amount_try),
                        COUNT(DISTINCT f.id)
                    FROM guven_fatura_tax t
                    JOIN guven_fatura f ON t.fatura_id = f.id
                    WHERE f.gvn_active = true AND f.direction = 'OUT'
                    GROUP BY 1, 2, 4

                    UNION ALL

                    SELECT
                        date_trunc('month', f.issue_date)::date,
                        t.tax_type,
                        '4_giden_logo',
                        f.company_id,
                        SUM(t.tax_amount_try),
                        COUNT(DISTINCT f.id)
                    FROM guven_fatura_tax t
                    JOIN guven_fatura f ON t.fatura_id = f.id
                    WHERE f.gvn_active = true
                        AND f.direction = 'OUT'
                        AND f.logo_fatura_count = 1
                    GROUP BY 1, 2, 4

                    UNION ALL

                    SELECT
                        date_trunc('month', f.issue_date)::date,
                        t.tax_type,
                        '5_gib_fark',
                        f.company_id,
                        SUM(CASE WHEN f.direction = 'OUT' THEN t.tax_amount_try
                                 WHEN f.direction = 'IN' THEN -t.tax_amount_try
                                 ELSE 0 END),
                        COUNT(DISTINCT f.id)
                    FROM guven_fatura_tax t
                    JOIN guven_fatura f ON t.fatura_id = f.id
                    WHERE f.gvn_active = true
                    GROUP BY 1, 2, 4

                    UNION ALL

                    SELECT
                        date_trunc('month', f.issue_date)::date,
                        t.tax_type,
                        '6_logo_fark',
                        f.company_id,
                        SUM(CASE WHEN f.direction = 'OUT' THEN t.tax_amount_try
                                 WHEN f.direction = 'IN' THEN -t.tax_amount_try
                                 ELSE 0 END),
                        COUNT(DISTINCT f.id)
                    FROM guven_fatura_tax t
                    JOIN guven_fatura f ON t.fatura_id = f.id
                    WHERE f.gvn_active = true
                        AND f.logo_fatura_count = 1
                    GROUP BY 1, 2, 4
                ),
                all_combos AS (
                    SELECT g.month_date, g.tax_type, g.company_id, k.kategori
                    FROM (SELECT DISTINCT month_date, tax_type, company_id
                          FROM raw_data) g
                    CROSS JOIN (VALUES
                        ('1_gelen_gib'), ('2_gelen_logo'), ('3_giden_gib'),
                        ('4_giden_logo'), ('5_gib_fark'), ('6_logo_fark')
                    ) AS k(kategori)
                )
                SELECT
                    row_number() OVER () AS id,
                    ac.month_date,
                    ac.tax_type,
                    ac.kategori,
                    ac.company_id,
                    COALESCE(r.tutar, 0) AS tutar,
                    COALESCE(r.fatura_sayisi, 0) AS fatura_sayisi
                FROM all_combos ac
                LEFT JOIN raw_data r USING (month_date, tax_type, company_id, kategori)
            )
        """ % self._table)
