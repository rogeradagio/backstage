from odoo import api, models


class StockQuant(models.Model):
    _inherit = "stock.quant"

    @api.model_create_multi
    def create(self, vals_list):
        quants = super().create(vals_list)
        quants.product_id._backstage_mark_dirty()
        return quants

    def write(self, vals):
        result = super().write(vals)
        if "quantity" in vals or "reserved_quantity" in vals:
            self.product_id._backstage_mark_dirty()
        return result
