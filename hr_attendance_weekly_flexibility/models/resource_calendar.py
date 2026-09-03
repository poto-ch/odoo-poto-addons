# Copyright 2026 Poto services numériques Sàrl
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

import logging
from collections import defaultdict
from datetime import datetime, time, timedelta

from dateutil.relativedelta import relativedelta
from pytz import timezone, utc

from odoo import fields, models
from odoo.osv import expression

from odoo.addons.hr_work_entry_contract.models.hr_work_intervals import WorkIntervals

_logger = logging.getLogger(__name__)


class ResourceCalendar(models.Model):
    _inherit = "resource.calendar"

    ignore_hours_per_day = fields.Boolean()

    def _attendance_intervals_batch(
        self, start_dt, end_dt, resources=None, domain=None, tz=None, lunch=False
    ):
        """
        Override to ignore hours per day completely
        """
        if not self.ignore_hours_per_day or not self.flexible_hours:
            return super()._attendance_intervals_batch(
                start_dt, end_dt, resources, domain, tz, lunch
            )
        assert self.ignore_hours_per_day and self.flexible_hours

        # From here, this is a reduxed version of _attendance_intervals_batch from
        # odoo.addons.resource.models.resource_calendar ResourceCalendar's
        assert start_dt.tzinfo and end_dt.tzinfo
        self.ensure_one()
        if not resources:
            resources = self.env["resource.resource"]
            resources_list = [resources]
        else:
            resources_list = list(resources) + [self.env["resource.resource"]]
        resource_ids = [r.id for r in resources_list]
        domain = domain if domain is not None else []
        domain = expression.AND(
            [
                domain,
                [
                    ("calendar_id", "=", self.id),
                    ("resource_id", "in", resource_ids),
                    ("display_type", "=", False),
                    ("day_period", "!=" if not lunch else "=", "lunch"),
                ],
            ]
        )

        # Since we only have one calendar to take in account
        # Group resources per tz they will all have the same result
        resources_per_tz = defaultdict(list)
        for resource in resources_list:
            resources_per_tz[tz or timezone((resource or self).tz)].append(resource)

        start = start_dt.astimezone(utc)
        end = end_dt.astimezone(utc)
        bounds_per_tz = {
            tz: (start_dt.astimezone(tz), end_dt.astimezone(tz))
            for tz in resources_per_tz.keys()
        }
        # Use the outer bounds from the requested timezones
        for _tz, bounds in bounds_per_tz.items():
            start = min(start, bounds[0].replace(tzinfo=utc))
            end = max(end, bounds[1].replace(tzinfo=utc))

        # Copy the result localized once per necessary timezone
        # Strictly speaking comparing start_dt < time or start_dt.astimezone(tz) < time
        # should always yield the same result. however while working with dates it is
        # easier if all dates have the same format
        resource_calendars = resources._get_calendar_at(start_dt, tz)
        result_per_resource_id = dict()
        for tz, tz_resources in resources_per_tz.items():
            start_datetime = start_dt.astimezone(tz)
            end_datetime = end_dt.astimezone(tz)

            for resource in tz_resources:
                # this is a calendar ignoring the hours per day
                # We create intervals to fill in the weekly intervals with the average
                # daily hours until the full time required hours are met. This gives us
                # the most correct approximation when looking at a daily and weekly
                # range for time offs and overtime calculations and work entry
                # generation
                start_date = start_datetime
                end_datetime_adjusted = end_datetime - relativedelta(seconds=1)
                end_date = end_datetime_adjusted

                calendar = resource_calendars[resource] if resource else self

                full_time_required_hours = calendar.full_time_required_hours
                # That's where this differs: this allows to consume all hours by friday,
                # ignoring the hours_per_day completely
                max_hours_per_day = calendar.full_time_required_hours / 5.0

                intervals = []
                # This is the running counter always set at mondays
                current_start_day = start_date - timedelta(days=start_date.weekday())

                while current_start_day <= end_date:
                    current_end_of_week = current_start_day + timedelta(days=6)

                    week_start = max(current_start_day, start_date)
                    week_end = min(current_end_of_week, end_date)

                    if current_start_day < start_date:
                        prior_days = (start_date - current_start_day).days
                        prior_hours = min(
                            full_time_required_hours, max_hours_per_day * prior_days
                        )
                    else:
                        prior_hours = 0

                    remaining_hours = max(0, full_time_required_hours - prior_hours)
                    remaining_hours = min(
                        remaining_hours, (end_dt - start_dt).total_seconds() / 3600
                    )

                    current_day = week_start
                    while current_day <= week_end:
                        day_start = tz.localize(datetime.combine(current_day, time.min))
                        day_end = tz.localize(datetime.combine(current_day, time.max))
                        day_period_start = max(start_datetime, day_start)
                        day_period_end = min(end_datetime, day_end)
                        allocate_hours = min(
                            max_hours_per_day,
                            remaining_hours,
                            (day_period_end - day_period_start).total_seconds() / 3600,
                        )
                        remaining_hours -= allocate_hours

                        # Create interval centered at 12:00 PM (or as close as possible)
                        midpoint = tz.localize(
                            datetime.combine(current_day, time(12, 0))
                        )
                        start_time = midpoint - timedelta(hours=allocate_hours / 2)
                        end_time = midpoint + timedelta(hours=allocate_hours / 2)

                        if start_time < day_period_start:
                            start_time = day_period_start
                            end_time = start_time + timedelta(hours=allocate_hours)
                        elif end_time > day_period_end:
                            end_time = day_period_end
                            start_time = end_time - timedelta(hours=allocate_hours)

                        dummy_attendance = self.env["resource.calendar.attendance"].new(
                            {
                                "duration_hours": allocate_hours,
                                "duration_days": 1,
                            }
                        )
                        _logger.warning(
                            f"{current_day.date()}: expected attendance is of "
                            f"{allocate_hours}h"
                        )

                        intervals.append((start_time, end_time, dummy_attendance))

                        current_day += timedelta(days=1)

                    current_start_day += timedelta(days=7)

                result_per_resource_id[resource.id] = WorkIntervals(intervals)

        return result_per_resource_id
