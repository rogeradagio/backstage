"""configura el trànsit i la recepció necessaris per a les recollides web a botiga."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    backstage_transit_location_id = fields.Many2one(
        "stock.location",
        string="Web transit",
        help="Transit location for transfers that feed web pickup orders of this store. "
             "It is not a Magento source: goods on the road are not for sale.",
    )
    backstage_receipt_type_id = fields.Many2one(
        "stock.picking.type",
        string="Web receipt",
        help="Receipt (transit → stock) the store validates when the goods of a web order arrive.",
    )
    backstage_pickup_ready = fields.Boolean(string="Pickup set up", compute="_compute_backstage_pickup_ready")

    @api.depends("backstage_transit_location_id", "backstage_receipt_type_id")
    def _compute_backstage_pickup_ready(self):
        for warehouse in self:
            warehouse.backstage_pickup_ready = bool(
                warehouse.backstage_transit_location_id and warehouse.backstage_receipt_type_id)

    def action_backstage_setup_pickup(self):
        """Crea (si no hi són) el trànsit i el tipus de recepció web d'aquesta botiga."""
        user = self.env.user
        if not (user.has_group("stock.group_stock_manager") or user.has_group("backstage.group_backstage_manager")):
            raise AccessError(_("Only inventory or Backstage administrators can set up store pickup."))
        for warehouse in self.sudo():
            if not warehouse.backstage_transit_location_id:
                parent = self.env.ref("stock.stock_location_locations", raise_if_not_found=False)
                warehouse.backstage_transit_location_id = self.env["stock.location"].create({
                    "name": _("Transit %s", warehouse.code),
                    "usage": "transit",
                    "location_id": parent.id if parent else False,
                    "company_id": warehouse.company_id.id,
                })
            if not warehouse.backstage_receipt_type_id:
                sequence = self.env["ir.sequence"].create({
                    "name": _("%s Web receipt", warehouse.code),
                    "prefix": "%s/RWEB/" % warehouse.code,
                    "padding": 5,
                    "company_id": warehouse.company_id.id,
                })
                warehouse.backstage_receipt_type_id = self.env["stock.picking.type"].create({
                    "name": _("Web receipt"),
                    "code": "incoming",
                    "sequence_code": "RWEB",
                    "sequence_id": sequence.id,
                    "warehouse_id": warehouse.id,
                    "company_id": warehouse.company_id.id,
                    "default_location_src_id": warehouse.backstage_transit_location_id.id,
                    "default_location_dest_id": warehouse.lot_stock_id.id,
                    "create_backorder": "ask",
                })
        return True
