"""desvia els traspassos web cap al trànsit de la botiga i marca l'estoc que cal reexportar."""

from odoo import models


def pickup_orders_for_moves(env, moves):
    """Comandes web de recollida a les quals serveixen aquests moviments."""
    orders = moves.sale_line_id.order_id | moves.picking_id.sale_id | moves.reference_ids.sale_ids
    origins = {o for o in moves.mapped("origin") + moves.picking_id.mapped("origin") if o}
    if origins:
        orders |= env["sale.order"].search([("name", "in", list(origins))])
    return orders.filtered(lambda o: o.magento_increment_id and o.magento_is_pickup)


class StockMove(models.Model):
    _inherit = "stock.move"

    def _action_confirm(self, merge=True, merge_into=False, create_proc=True):
        self._backstage_route_web_supply_to_transit()
        return super()._action_confirm(merge=merge, merge_into=merge_into, create_proc=create_proc)

    def _backstage_route_web_supply_to_transit(self):
        """Traspàs d'un altre magatzem cap a l'estoc d'una botiga amb recepció web, per a una comanda web
        de recollida → el destí passa a ser el trànsit de la botiga (§5.3). La reposició normal no es toca."""
        for move in self.filtered(lambda m: m.state == "draft" and m.location_id.usage == "internal"):
            warehouse = move.location_dest_id.warehouse_id
            if not warehouse.backstage_pickup_ready:
                continue
            if move.location_dest_id != warehouse.lot_stock_id or move.location_id.warehouse_id == warehouse:
                continue
            orders = pickup_orders_for_moves(self.env, move).filtered(lambda o: o.warehouse_id == warehouse)
            if not orders:
                continue
            transit = warehouse.backstage_transit_location_id
            move.location_dest_id = transit
            picking = move.picking_id
            if picking and all(m.location_dest_id == transit for m in picking.move_ids):
                picking.write({"location_dest_id": transit.id, "backstage_web_supply": True})

    # ------------------------------------------------------------------ estoc a exportar

    def _action_assign(self, force_qty=False):
        result = super()._action_assign(force_qty=force_qty)
        self.product_id._backstage_mark_dirty()
        return result

    def _do_unreserve(self):
        result = super()._do_unreserve()
        self.product_id._backstage_mark_dirty()
        return result

    def _action_done(self, cancel_backorder=False):
        result = super()._action_done(cancel_backorder=cancel_backorder)
        self.product_id._backstage_mark_dirty()
        return result

    def _action_cancel(self):
        result = super()._action_cancel()
        self.product_id._backstage_mark_dirty()
        return result
