from odoo import _, fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    backstage_stock_sync = fields.Boolean(
        string="Export stock to Magento",
        default=True,
        help="Untick to stop sending the stock of this product to the Magento sources.",
    )
    backstage_stock_visible = fields.Boolean(compute="_compute_backstage_stock_visible")

    def _compute_backstage_stock_visible(self):
        enabled = self.env["backstage.backend"]._get().stock_enabled
        for template in self:
            template.backstage_stock_visible = bool(enabled and template.is_storable)

    def action_backstage_stock_preview(self):
        self.ensure_one()
        preview = self.env["backstage.stock.preview"].create({"product_tmpl_id": self.id})
        return {
            "type": "ir.actions.act_window",
            "name": _("Stock in Magento"),
            "res_model": preview._name,
            "res_id": preview.id,
            "view_mode": "form",
            "target": "new",
        }
