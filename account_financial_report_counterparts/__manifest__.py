# Copyright 2026 Poto services numériques Sàrl - Didier Raboud
# Part of Odoo. See LICENSE file for full copyright and licensing details.
{
    "name": "Account Financial Reports - Display counterparts",
    "version": "18.0.0.0.0",
    "author": "Poto services numériques Sàrl, Odoo Community Association (OCA)",
    "website": "https://github.com/poto-ch/odoo-poto-addons",
    "license": "AGPL-3",
    "category": "Reporting",
    "depends": ["account_financial_report", "account"],
    "data": [
        "views/report_general_ledger_lines_counterpart.xml",
    ],
    "installable": True,
    "maintainers": ["OdyX"],
}
