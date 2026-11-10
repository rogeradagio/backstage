from odoo import fields, models


class ProductProduct(models.Model):
    _inherit = "product.product"

    backstage_stock_dirty = fields.Boolean(copy=False, index=True, help="Stock must be exported to Magento again.")

    def _backstage_mark_dirty(self):
        products = self.filtered(lambda p: p.is_storable and p.default_code and not p.backstage_stock_dirty)
        if products:
            products.sudo().write({"backstage_stock_dirty": True})
