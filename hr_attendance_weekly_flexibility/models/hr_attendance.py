import logging
from collections import defaultdict
from datetime import datetime, timedelta
from operator import itemgetter

import pytz
from dateutil.relativedelta import MO, SU, relativedelta

from odoo import models
from odoo.osv import expression
from odoo.osv.expression import AND, OR
from odoo.tools.float_utils import float_compare, float_is_zero

_logger = logging.getLogger(__name__)


class HrAttendance(models.Model):
    _inherit = "hr.attendance"

    def _update_overtime(self, employee_attendance_dates=None):  # noqa: C901
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

        flexible_emp_data = self._collect_week_boundaries_per_flexible_employee(
            employee_attendance_dates
        )

        if flexible_emp_data:
            att_groups = self.env["hr.attendance"]._read_group(
                domain=[
                    ("employee_id", "in", [emp.id for emp in flexible_emp_data]),
                    (
                        "check_in",
                        ">=",
                        min(d["min_utc"] for d in flexible_emp_data.values()),
                    ),
                    (
                        "check_in",
                        "<=",
                        max(d["max_utc"] for d in flexible_emp_data.values()),
                    ),
                ],
                groupby=["employee_id"],
                aggregates=["id:recordset"],
            )
            att_by_emp = dict(att_groups)

            for emp, emp_data in flexible_emp_data.items():
                week_dates_to_add = set()
                for att in att_by_emp.get(emp, self.browse()):
                    day_start_tuple = att._get_day_start_and_day(emp, att.check_in)
                    week_start = day_start_tuple[1] + relativedelta(weekday=MO(-1))
                    if week_start in emp_data["week_starts"]:
                        week_dates_to_add.add(day_start_tuple)
                expanded_attendance_dates[emp] = (
                    expanded_attendance_dates.get(emp, set()) | week_dates_to_add
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
            all_attendances = self.env["hr.attendance"].search(attendance_domain)
            for attendance in all_attendances:
                check_in_day_start = attendance._get_day_start_and_day(
                    attendance.employee_id, attendance.check_in
                )
                attendances_per_day[check_in_day_start[1]] += attendance

            # As _attendance_intervals_batch and _leave_intervals_batch both take
            # localized dates we need to localize those date
            start = pytz.utc.localize(min(attendance_dates, key=itemgetter(0))[0])
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
                    assert calendar.required_hours_are_weekly

                    hours_today = sum(attendances.mapped("worked_hours"))
                    today_working_times = working_times.get(attendance_date)
                    try:
                        due_hours_today = (
                            today_working_times[0][1] - today_working_times[0][0]
                        ).total_seconds() / 3600
                    except TypeError:
                        due_hours_today = 0.0

                    # Find the last attendance before this day
                    try:
                        # latest day with attendances.
                        # So: tere was no work in the days between
                        latest_attended_day = max(
                            [
                                day
                                for day in attendances_per_day.keys()
                                if day < attendance_date
                            ]
                        )
                        # Which working days were concerned
                        working_times_since = {
                            day: wt
                            for day, wt in working_times.items()
                            if day < attendance_date and day > latest_attended_day
                        }
                        # That amount of work was not done
                        missed_working_time = sum(
                            [
                                wt[0][1] - wt[0][0]
                                for wt in working_times_since.values()
                            ],
                            timedelta(),
                        )
                    except ValueError:
                        missed_working_time = timedelta()

                    # Overtime is:
                    overtime_duration = (
                        hours_today
                        - due_hours_today
                        - (missed_working_time.total_seconds() / 3600)
                    )
                    overtime_duration_real = overtime_duration

                    _logger.warning(
                        f"{attendance_date}   "
                        f"{due_hours_today}   "
                        f"{round(hours_today, 2)}   "
                        f"({round(overtime_duration_real, 2)})"
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

    def _collect_week_boundaries_per_flexible_employee(self, employee_attendance_dates):
        """
        Collect week boundaries per flexible employee, then fetch all attendances
        in a single query
        """
        # This is from Odoo 18.0's HrAttendance _update_overtime
        flexible_emp_data = {}
        for emp in list(employee_attendance_dates.keys()):
            calendar = emp.resource_calendar_id or emp.company_id.resource_calendar_id
            if (
                calendar
                and calendar.flexible_hours
                and calendar.full_time_required_hours
            ):
                employee_tz = pytz.timezone(emp._get_tz())
                week_starts = set()
                for attendance_tuple in employee_attendance_dates[emp]:
                    attendance_date = attendance_tuple[1]
                    week_starts.add(attendance_date + relativedelta(weekday=MO(-1)))

                if week_starts:
                    min_week_start = min(week_starts)
                    max_week_start = max(week_starts)
                    min_week_start_utc = (
                        employee_tz.localize(
                            datetime.combine(min_week_start, datetime.min.time())
                        )
                        .astimezone(pytz.utc)
                        .replace(tzinfo=None)
                    )
                    max_week_end_utc = (
                        employee_tz.localize(
                            datetime.combine(
                                max_week_start + relativedelta(weekday=SU(1)),
                                datetime.max.time(),
                            )
                        )
                        .astimezone(pytz.utc)
                        .replace(tzinfo=None)
                    )
                    flexible_emp_data[emp] = {
                        "week_starts": week_starts,
                        "min_utc": min_week_start_utc,
                        "max_utc": max_week_end_utc,
                    }
        return flexible_emp_data
