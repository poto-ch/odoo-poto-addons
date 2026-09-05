import logging
from collections import defaultdict
from datetime import datetime, time, timedelta
from operator import itemgetter

import pytz
from dateutil.relativedelta import TU, relativedelta

from odoo import models
from odoo.osv import expression
from odoo.osv.expression import AND, OR
from odoo.tools.float_utils import float_compare, float_is_zero

_logger = logging.getLogger(__name__)
_logger.setLevel(logging.ERROR)


class HrAttendance(models.Model):
    _inherit = "hr.attendance"

    def _update_overtime(self, employee_attendance_dates=None):  # noqa: C901
        """
        This is mostly a copy from hr.attendance _update_overtime as there's no other
        neat way to do this (yet?)
        """
        if employee_attendance_dates is None:
            employee_attendance_dates = self._get_attendances_dates()
        employee_attendance_dates = {
            employee: attendance_dates
            for employee, attendance_dates in employee_attendance_dates.items()
            if not employee.is_fully_flexible
        }
        expanded_attendance_dates = dict(employee_attendance_dates)

        # If no calendar is affected by our module, just super()
        custom = False
        for emp in list(employee_attendance_dates.keys()):
            calendar = emp.resource_calendar_id or emp.company_id.resource_calendar_id
            if calendar.required_hours_are_weekly:
                custom = True
                break
        if not custom:
            return super()._update_overtime(employee_attendance_dates)

        # Alse examine attendances from the current week
        days_to_add = set()
        for emp, attendance_dates in employee_attendance_dates.items():
            first_in_batch = min(attendance_dates)[1]
            monday = (
                first_in_batch + relativedelta(weekday=TU(-1)) + relativedelta(days=-1)
            )
            for att in self.env["hr.attendance"].search(
                domain=[
                    ("employee_id", "=", emp.id),
                    ("check_in", ">=", monday),
                    ("check_in", "<", first_in_batch),
                ],
            ):
                previous_day_tuple = att._get_day_start_and_day(emp, att.check_in)
                days_to_add.add(previous_day_tuple)

            expanded_attendance_dates[emp] = (
                expanded_attendance_dates.get(emp, set()) | days_to_add
            )

        employee_attendance_dates = expanded_attendance_dates

        overtime_to_unlink = self.env["hr.attendance.overtime"]
        overtime_vals_list = []
        affected_employees = self.env["hr.employee"]
        for emp, attendance_dates in employee_attendance_dates.items():
            # get_attendances_dates returns the date translated from the local
            # timezone without tzinfo, and contains all the date which we need
            # to check for overtime
            attendance_domain = expression.FALSE_DOMAIN
            for attendance_date in attendance_dates:
                attendance_domain = OR(
                    [
                        attendance_domain,
                        [
                            ("check_in", ">=", attendance_date[0]),
                            ("check_in", "<", attendance_date[0] + timedelta(hours=24)),
                        ],
                    ]
                )
            attendance_domain = AND([[("employee_id", "=", emp.id)], attendance_domain])

            # Attendances per LOCAL day
            attendances_per_day = defaultdict(lambda: self.env["hr.attendance"])
            all_attendances = self.env["hr.attendance"].search(
                attendance_domain, order="check_in ASC"
            )
            for attendance in all_attendances:
                check_in_day_start = attendance._get_day_start_and_day(
                    attendance.employee_id, attendance.check_in
                )
                attendances_per_day[check_in_day_start[1]] += attendance

            # As _attendance_intervals_batch and _leave_intervals_batch both take
            # localized dates we need to localize those date
            # Make sure to use the start of the contract, not the earliest attendance
            start = pytz.utc.localize(
                datetime.combine(
                    emp.contract_id.date_start,
                    time(0, 0),
                )
            )
            stop = pytz.utc.localize(
                max(attendance_dates, key=itemgetter(0))[0] + timedelta(hours=24)
            )

            # Retrieve expected attendance intervals
            calendar = emp.resource_calendar_id or emp.company_id.resource_calendar_id
            expected_attendances = emp._employee_attendance_intervals(start, stop)

            # working_times = {date: [(start, stop)]}
            working_times = defaultdict(lambda: [])
            for expected_attendance in expected_attendances:
                # Exclude resource.calendar.attendance
                working_times[expected_attendance[0].date()].append(
                    expected_attendance[:2]
                )

            overtimes = (
                self.env["hr.attendance.overtime"]
                .sudo()
                .search(
                    [
                        ("employee_id", "=", emp.id),
                        ("date", "in", [day_data[1] for day_data in attendance_dates]),
                    ]
                )
            )

            assert bool(calendar and calendar.required_hours_are_weekly)

            # Loop through each day of attendances, and compute the day over/undertime.
            for day_data in sorted(attendance_dates, key=lambda x: x[1]):
                attendance_date = day_data[1]

                attendances = attendances_per_day.get(attendance_date, self.browse())
                unfinished_shifts = attendances.filtered(lambda a: not a.check_out)
                overtime_duration = 0
                overtime_duration_real = 0
                # Overtime is not counted if any shift is not closed or if there are
                # no attendances for that day, this could happen when deleting
                # attendances.

                if not unfinished_shifts and attendances:
                    hours_today = sum(attendances.mapped("worked_hours"))
                    today_working_times = working_times.get(attendance_date)
                    missed_working_hours = 0.0
                    latest_missed_day = None

                    try:
                        due_hours_today = (
                            today_working_times[0][1] - today_working_times[0][0]
                        ).total_seconds() / 3600
                    except TypeError:
                        due_hours_today = 0.0

                    # Find the last attendance before this day in the batch
                    earlier_days = [
                        day
                        for day in attendances_per_day.keys()
                        if day < attendance_date
                    ]

                    if earlier_days:
                        latest_missed_day = max(earlier_days) + timedelta(days=1)
                    elif not (
                        self.env["hr.attendance"].search_count(
                            domain=[
                                ("employee_id", "=", emp.id),
                                ("check_in", "<", attendance_date),
                                ("check_in", ">=", start.date()),
                            ]
                        )
                    ):
                        # There was no earlier attendance, the work should have
                        # happened since contract start
                        latest_missed_day = start.date()

                    if latest_missed_day:
                        # Which working days were concerned
                        working_times_since = {
                            day: wt
                            for day, wt in working_times.items()
                            if day < attendance_date and day >= latest_missed_day
                        }

                        # That amount of work was not done
                        missed_working_hours = (
                            sum(
                                [
                                    wt[0][1] - wt[0][0]
                                    for wt in working_times_since.values()
                                ],
                                timedelta(),
                            ).total_seconds()
                            / 3600
                        )

                        # Overtime is:
                        overtime_duration = (
                            hours_today - due_hours_today - (missed_working_hours)
                        )
                        overtime_duration_real = overtime_duration

                        _logger.debug(
                            f"{attendance_date}   "
                            f"due : {due_hours_today}   "
                            f"done: {round(hours_today, 2)}   "
                            "over: "
                            f"({round(hours_today - due_hours_today, 2)} - "
                            f"{missed_working_hours}) = "
                            f"{round(overtime_duration_real, 2)}"
                        )

                overtime = overtimes.filtered(
                    lambda o, attendance_date=attendance_date: o.date == attendance_date
                )
                if not float_is_zero(overtime_duration, 2) or unfinished_shifts:
                    # Do not create if any attendance doesn't have a check_out,
                    # update if exists
                    if unfinished_shifts:
                        overtime_duration = 0
                    if not overtime and overtime_duration:
                        overtime_vals_list.append(
                            {
                                "employee_id": emp.id,
                                "date": attendance_date,
                                "duration": overtime_duration,
                                "duration_real": overtime_duration_real,
                            }
                        )
                    elif overtime:
                        overtime.sudo().write(
                            {
                                "duration": overtime_duration,
                                "duration_real": overtime_duration,
                            }
                        )
                        affected_employees |= overtime.employee_id
                elif overtime:
                    overtime_to_unlink |= overtime
        created_overtimes = (
            self.env["hr.attendance.overtime"].sudo().create(overtime_vals_list)
        )
        employees_worked_hours_to_compute = (
            affected_employees.ids
            + created_overtimes.employee_id.ids
            + overtime_to_unlink.employee_id.ids
        )
        overtime_to_unlink.sudo().unlink()
        to_recompute = self.search(
            [("employee_id", "in", employees_worked_hours_to_compute)]
        )
        # for automatically validated attendances, avoid recomputing extra hours
        # if user has changed its value
        validated_modified = to_recompute.filtered(
            lambda att: att.employee_id.company_id.attendance_overtime_validation
            == "no_validation"
            and float_compare(
                att.overtime_hours, att.validated_overtime_hours, precision_digits=2
            )
        )
        self.env.add_to_compute(self._fields["overtime_hours"], to_recompute)
        self.env.add_to_compute(
            self._fields["validated_overtime_hours"], to_recompute - validated_modified
        )
        self.env.add_to_compute(self._fields["expected_hours"], to_recompute)
