# Copyright 2026 Poto services numériques Sàrl - Didier Raboud
# Part of Odoo. See LICENSE file for full copyright and licensing details.
{
    "name": "Display rounded times for timesheets in portal",
    "version": "18.0.0.0.0",
    "author": "Poto services numériques Sàrl, Odoo Community Association (OCA)",
    "website": "https://github.com/poto-ch/odoo-poto-addons",
    "license": "AGPL-3",
    "category": "Sales",
    "depends": ["hr_timesheet", "sale_timesheet_rounded"],
    "data": [
        "views/portal_my_timesheets_rounded.xml",
    ],
    "installable": True,
    "maintainers": ["OdyX"],
}
