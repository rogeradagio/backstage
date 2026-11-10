"""mostra, per a un producte, què envia Backstage a cada font de Magento i per què."""

from odoo import _, api, fields, models


class BackstageStockPreview(models.TransientModel):
    _name = "backstage.stock.preview"
    _description = "Stock in Magento"

    product_tmpl_id = fields.Many2one("product.template", required=True, readonly=True)
    stock_sync = fields.Boolean(related="product_tmpl_id.backstage_stock_sync")
    missing_sku = fields.Boolean(compute="_compute_status")
    pending_export = fields.Boolean(compute="_compute_status")
    line_ids = fields.One2many("backstage.stock.preview.line", "preview_id", readonly=True)

    @api.depends("product_tmpl_id")
    def _compute_status(self):
        for preview in self:
            variants = preview.product_tmpl_id.product_variant_ids
            preview.missing_sku = any(not v.default_code for v in variants)
            preview.pending_export = any(variants.mapped("backstage_stock_dirty"))

    @api.model_create_multi
    def create(self, vals_list):
        previews = super().create(vals_list)
        Stock = self.env["backstage.stock"]
        for preview in previews:
            preview._fill_lines(Stock)
        return previews

    def _fill_lines(self, Stock):
        products = self.product_tmpl_id.product_variant_ids
        sources = Stock._export_sources()
        breakdown = Stock.compute_breakdown(products, sources)
        self.line_ids = [(0, 0, {
            "product_id": product.id,
            "source_id": source.id,
            **breakdown[(product.id, source.source_code)],
        }) for product in products for source in sources if (product.id, source.source_code) in breakdown]

    def action_toggle_sync(self):
        self.ensure_one()
        template = self.product_tmpl_id
        template.backstage_stock_sync = not template.backstage_stock_sync
        template.product_variant_ids.write({"backstage_stock_dirty": template.backstage_stock_sync})
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "name": _("Stock in Magento"),
        }


class BackstageStockPreviewLine(models.TransientModel):
    _name = "backstage.stock.preview.line"
    _description = "Stock in Magento per source"

    preview_id = fields.Many2one("backstage.stock.preview", required=True, ondelete="cascade")
    product_id = fields.Many2one("product.product", string="Variant", readonly=True)
    source_id = fields.Many2one("backstage.source", string="Source", readonly=True)
    on_hand = fields.Float(string="On hand", digits="Product Unit", readonly=True)
    reserved_other = fields.Float(
        string="Reserved for other sales", digits="Product Unit", readonly=True,
        help="Reserved in Odoo for orders that do not come from Magento.",
    )
    shipped_unsent = fields.Float(
        string="Shipped, not yet in Magento", digits="Product Unit", readonly=True,
        help="Already shipped in Odoo, but Magento does not know yet: it still holds them as reserved.",
    )
    sellable = fields.Float(string="Sent to Magento", digits="Product Unit", readonly=True)
