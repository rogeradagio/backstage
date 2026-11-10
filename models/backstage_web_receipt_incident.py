"""registra i resol les diferències detectades en la recepció de traspassos web."""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

REASONS = [
    ("not_received", "Not received"),
    ("short", "Fewer units"),
    ("over", "More units"),
    ("unexpected", "Unexpected product"),
    ("damaged", "Damaged"),
    ("wrong_item", "Wrong product"),
    ("other", "Other"),
    ("unspecified", "No reason given"),
]
# Faltants: es tanquen sols quan la mercaderia arriba en una recepció posterior.
SELF_RESOLVING_REASONS = ("short", "not_received", "unspecified")

PRECISION = 0.0001


def suggested_reason(expected, received):
    if expected <= PRECISION:
        return "unexpected"
    if received <= PRECISION:
        return "not_received"
    return "short" if received < expected else "over"


class BackstageWebReceiptIncident(models.Model):
    """Diferència entre el que el magatzem d'origen va enviar i el que la botiga ha rebut (§5.3)."""

    _name = "backstage.web.receipt.incident"
    _description = "Web receipt incident"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    picking_id = fields.Many2one("stock.picking", string="Receipt", required=True, ondelete="cascade", index=True)
    supply_picking_id = fields.Many2one(
        related="picking_id.backstage_supply_picking_id", string="Original transfer", store=True)
    warehouse_id = fields.Many2one(related="picking_id.picking_type_id.warehouse_id", string="Store", store=True)
    sale_order_id = fields.Many2one("sale.order", string="Web order", index=True)
    product_id = fields.Many2one("product.product", required=True)
    qty_expected = fields.Float(string="Sent", digits="Product Unit")
    qty_received = fields.Float(string="Received", digits="Product Unit")
    difference = fields.Float(compute="_compute_difference", store=True, digits="Product Unit")
    reason = fields.Selection(REASONS, required=True, default="unspecified", tracking=True)
    note = fields.Text(string="Store comment")
    state = fields.Selection(
        [("draft", "Draft"), ("open", "Open"), ("resolved", "Resolved")],
        default="draft", required=True, index=True, tracking=True,
        help="Draft: declared in the wizard, but the receipt is not validated yet.",
    )
    resolution = fields.Text(tracking=True, help="What was done about it.")
    resolved_by_id = fields.Many2one("res.users", string="Resolved by", readonly=True)
    resolved_date = fields.Datetime(string="Resolved on", readonly=True)

    @api.depends("picking_id.name", "product_id.display_name")
    @api.depends_context("lang")
    def _compute_display_name(self):
        for incident in self:
            incident.display_name = "%s · %s" % (incident.picking_id.name or "", incident.product_id.display_name or "")

    @api.depends("qty_expected", "qty_received")
    def _compute_difference(self):
        for incident in self:
            incident.difference = incident.qty_received - incident.qty_expected

    def action_resolve(self):
        if any(not incident.resolution for incident in self):
            raise UserError(_("Write down how the incident was resolved."))
        self.write({"state": "resolved", "resolved_by_id": self.env.uid, "resolved_date": fields.Datetime.now()})
        for order in self.sale_order_id:
            order.message_post(body=_(
                "Receipt incidents resolved: %s",
                ", ".join(self.filtered(lambda i: i.sale_order_id == order).product_id.mapped("display_name")),
            ))
        return True

    def action_reopen(self):
        self.write({"state": "open", "resolved_by_id": False, "resolved_date": False})
        return True
