import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from operator import itemgetter
from zoneinfo import ZoneInfo

from dateutil.relativedelta import MO, relativedelta

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
        for emp, attendance_dates in employee_attendance_dates.items():
            days_to_add = set()
            # Find the latest attendance before the first, bump to its earliest monday

            # Get the start of the day of the first attendance in batch, localized in
            # the employee TZ.
            start_of_first_day_in_batch = self._get_localized_start_of_day(
                emp, min(attendance_dates)[1]
            )

            latest_attendance_before_batch = self.env["hr.attendance"].search(
                domain=[
                    ("employee_id", "=", emp.id),
                    (
                        "check_in",
                        "<",
                        self._get_dt_in_naive(start_of_first_day_in_batch),
                    ),
                ],
                order="check_in DESC",
                limit=1,
            )
            if not latest_attendance_before_batch:
                # Handling the first attendance ever, don't add anything
                continue

            # Localize in the employee timezone
            latest_attendance = self._get_dt_as_employee(
                emp, latest_attendance_before_batch.check_in
            )
            # Find the previous monday. Need to be localized again, in case it jumps
            # over a summer-time saving change
            monday_before_latest = self._get_localized_start_of_day(
                emp, latest_attendance + relativedelta(weekday=MO(-1))
            )
            _logger.debug(f"Start of the previous monday: {monday_before_latest}")

            # Find all attendances between the monday and now.
            # There must be at least one
            for att in self.env["hr.attendance"].search(
                domain=[
                    ("employee_id", "=", emp.id),
                    (
                        "check_in",
                        "<",
                        self._get_dt_in_naive(start_of_first_day_in_batch),
                    ),
                    ("check_in", ">=", self._get_dt_in_naive(monday_before_latest)),
                ]
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
            start = min(attendance_dates, key=itemgetter(0))[0].astimezone(timezone.utc)
            stop = (
                max(attendance_dates, key=itemgetter(0))[0] + timedelta(hours=24)
            ).astimezone(timezone.utc)

            # Retrieve expected attendance intervals
            working_times = self._get_working_times(emp, start, stop)

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

            # Get all contracts covering the time span
            emp_contracts = emp._get_contracts(
                date_from=start, date_to=stop, states=["open", "close", "cancel"]
            )

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

                # Get contract start _for that date_
                emp_contract_start = self._get_localized_start_of_day(
                    emp,
                    next(
                        c
                        for c in emp_contracts
                        if attendance_date >= c.date_start
                        and (not c.date_end or attendance_date < c.date_end)
                    ).date_start,
                )

                if not unfinished_shifts and attendances:
                    hours_today = sum(attendances.mapped("worked_hours"))
                    today_working_times = working_times.get(attendance_date)
                    missed_working_hours = 0.0
                    latest_missed_date = None

                    try:
                        due_hours_today = (
                            today_working_times[0][1] - today_working_times[0][0]
                        ).total_seconds() / 3600.0
                    except TypeError:
                        due_hours_today = 0.0

                    # Find the last attendance before this day in the batch
                    earlier_days = [
                        day for day in attendances_per_day if day < attendance_date
                    ]

                    if earlier_days:
                        # First day without work
                        latest_missed_date = max(earlier_days) + timedelta(days=1)
                    elif (
                        self.env["hr.attendance"].search_count(
                            domain=[
                                ("employee_id", "=", emp.id),
                                (
                                    "check_in",
                                    ">=",
                                    self._get_dt_in_naive(emp_contract_start),
                                ),
                                (
                                    "check_in",
                                    "<",
                                    self._get_utc_start_of_day(emp, attendance_date),
                                ),
                            ]
                        )
                    ) == 0:
                        # There was no earlier attendance, the work should have
                        # happened since contract start.
                        latest_missed_datetime = emp_contract_start

                        _logger.debug(
                            "No earlier attendance, so check from contract start "
                            f"{latest_missed_datetime}"
                        )
                        # Now refetch
                        working_times = self._get_working_times(
                            emp, latest_missed_datetime, stop
                        )
                        latest_missed_date = latest_missed_datetime.date()

                    if latest_missed_date:
                        # Which working days were concerned
                        working_times_since = {
                            day: wt
                            for day, wt in working_times.items()
                            if day < attendance_date and day >= latest_missed_date
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
                            / 3600.0
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

    def _get_working_times(self, emp, start, stop):
        """
        Get "due" working times for employee in a dates' interval
        """
        expected_attendances = emp._employee_attendance_intervals(start, stop)

        # working_times = {date: [(start, stop)]}
        working_times = defaultdict(list)
        for expected_attendance in expected_attendances:
            # Exclude resource.calendar.attendance
            working_times[expected_attendance[0].date()].append(expected_attendance[:2])
        return working_times

    def _get_localized_start_of_day(self, emp, dt):
        """
        Get the start of day from the employees' perspective, in their timezone
        """
        return datetime.combine(dt, datetime.min.time()).replace(
            tzinfo=ZoneInfo(emp._get_tz())
        )

    def _get_dt_in_naive(self, dt):
        """
        Get the dt in UTC and naive
        """
        return dt.astimezone(timezone.utc).replace(tzinfo=None)

    def _get_utc_start_of_day(self, emp, dt):
        """
        Get the start of day from the employees' perspective
        """
        return self._get_dt_in_naive(self._get_localized_start_of_day(emp, dt))

    def _get_dt_as_employee(self, emp, dt):
        """
        Get the datetime from the employees' perspective
        """
        return dt.astimezone(ZoneInfo(emp._get_tz()))
