import logging

from odoo import models

_logger = logging.getLogger(__name__)


class HrAttendance(models.Model):
    _inherit = "hr.attendance"

    def _update_overtime(self, employee_attendance_dates=None):
        _logger.warning("_update_overtime")
        return super()._update_overtime(employee_attendance_dates)
