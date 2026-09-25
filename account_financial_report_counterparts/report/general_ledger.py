# Copyright 2026 Poto services numériques Sàrl
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from odoo import api, models


class GeneralLedgerReport(models.AbstractModel):
    _inherit = "report.account_financial_report.general_ledger"

    def _get_ml_fields(self):
        return super()._get_ml_fields() + ["counterpart_account_codes"]

    @api.model
    def _get_move_line_data(self, move_line):
        move_line_data = super()._get_move_line_data(move_line)
        move_line_data.update(
            {"counterpart_account_codes": move_line["counterpart_account_codes"]}
        )
        return move_line_data
