# Copyright 2026 Poto services numériques Sàrl
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from odoo import fields, models


class ResourceCalendar(models.Model):
    _inherit = "resource.calendar"

    ignore_hours_per_day = fields.Boolean()
