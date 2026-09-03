# Copyright 2026 Poto services numériques Sàrl
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from odoo import api, fields, models
from odoo.tools.float_utils import float_round


class ResourceCalendar(models.Model):
    _inherit = "resource.calendar"

    required_hours_are_weekly = fields.Boolean(
        "Required hours apply weekly",
        default=False,
        help="If set, the average daily hours' setting is ignored "
        "in favour of required hours per week. This allows "
        "employees to register attendance at anytime.",
    )

    @api.depends(
        "attendance_ids",
        "attendance_ids.hour_from",
        "attendance_ids.hour_to",
        "two_weeks_calendar",
        "flexible_hours",
    )
    def _compute_hours_per_day(self):
        for calendar in self:
            if calendar.flexible_hours:
                if calendar.required_hours_are_weekly:
                    # This module assumes there are 5 working days per week
                    calendar.hours_per_day = float_round(
                        calendar.full_time_required_hours / float(5), precision_digits=2
                    )
                continue
            attendances = calendar._get_global_attendances()
            calendar.hours_per_day = calendar._get_hours_per_day(attendances)
