from odoo import fields, models


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    magento_order_item_id = fields.Integer(
        string="Magento item",
        copy=False,
        help="item_id of the Magento order line. Needed to mirror invoices and shipments.",
    )
