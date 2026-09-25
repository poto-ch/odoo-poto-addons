# Copyright 2026 Poto services numériques Sàrl
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)


from odoo import api, fields, models


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    counterpart_account_codes = fields.Char(
        "Counterpart Account codes", compute="_compute_counterpart_account_codes"
    )

    @api.depends("move_id", "move_id.line_ids")
    def _compute_counterpart_account_codes(self):
        for line in self:
            line.counterpart_account_codes = "|".join(
                sorted(
                    [
                        c.code
                        for c in line.move_id.line_ids.account_id
                        if c.code != line.account_id.code
                    ]
                )
            )
