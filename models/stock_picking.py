"""albarans: esdeveniments de lliurament i devolució, i traspassos web en dos passos amb incidències."""

from collections import defaultdict

from odoo import _, fields, models

from .backstage_web_receipt_incident import PRECISION, REASONS, SELF_RESOLVING_REASONS, suggested_reason
from .stock_move import pickup_orders_for_moves

DELIVERED_STATE = "customer_delivered"
INCIDENTS_DECLARED_KEY = "backstage_incidents_declared"


class StockPicking(models.Model):
    _inherit = "stock.picking"

    backstage_web_supply = fields.Boolean(
        copy=False, help="First leg of a two-step web transfer (origin → transit).")
    backstage_supply_picking_id = fields.Many2one(
        "stock.picking", copy=True, string="Original transfer", index=True,
        help="Web receipt: the transfer it comes from. Copied to the backorders of the receipt.",
    )
    backstage_incident_ids = fields.One2many(
        "backstage.web.receipt.incident", "picking_id", string="Receipt incidents")
    backstage_incident_count = fields.Integer(compute="_compute_backstage_incident_count")

    def _compute_backstage_incident_count(self):
        for picking in self:
            picking.backstage_incident_count = len(
                picking.backstage_incident_ids.filtered(lambda i: i.state != "draft"))

    def action_view_backstage_incidents(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("backstage.action_backstage_web_receipt_incident")
        action.update(domain=[("picking_id", "=", self.id), ("state", "!=", "draft")], context={})
        return action

    # ------------------------------------------------------------------ disparadors

    def _action_done(self):
        result = super()._action_done()
        for picking in self:
            picking._backstage_emit_shipment()
            picking._backstage_emit_return()
            if picking.backstage_web_supply and picking.location_dest_id.usage == "transit":
                picking._backstage_web_supply_departed()
            elif picking.backstage_supply_picking_id:
                picking._backstage_web_receipt_validated()
        return result

    def write(self, vals):
        result = super().write(vals)
        # El camp delivery_state el defineix el seguiment de transportista d'OCA, opcional aquí.
        if vals.get("delivery_state") == DELIVERED_STATE:
            for picking in self:
                picking._backstage_emit_delivered()
        return result

    # ------------------------------------------------------------------ lliurament, devolució

    def _backstage_shipment_items(self):
        """Línies expedides amb la seva línia Magento. Retorna (items, errors)."""
        self.ensure_one()
        items, errors = [], []
        for move in self.move_ids.filtered(lambda m: m.state == "done" and m.sale_line_id and m.quantity > 0):
            line = move.sale_line_id
            if not line.magento_order_item_id:
                errors.append(move.product_id.display_name)
                continue
            items.append({
                "order_item_id": line.magento_order_item_id,
                "sku": move.product_id.default_code or "",
                "qty": move.quantity,
            })
        return items, errors

    def _backstage_missing_items(self, order, errors):
        order._backstage_notify(
            _("Magento: %s not sent", self.name),
            _("%(doc)s was not sent to Magento: these products have no Magento line (%(products)s). "
              "Link the order again from its Magento tab.", doc=self.name, products=", ".join(errors)),
        )

    def _backstage_emit_shipment(self):
        """Lliurament al client validat → collected (recollida) o shipment_dispatched (enviament)."""
        self.ensure_one()
        order = self.sale_id
        if not order or not order.magento_increment_id:
            return
        if self.picking_type_code != "outgoing" or self.location_dest_id.usage != "customer":
            return
        if not order._backstage_link_or_defer(self, "_backstage_emit_shipment"):
            return
        items, errors = self._backstage_shipment_items()
        if errors:
            return self._backstage_missing_items(order, errors)
        if not items:
            return
        source = self.env["backstage.source"].source_for_location(self.location_id)
        payload = {"leg_ref": self.name, "source_code": source.source_code or "", "items": items}
        if order.magento_is_pickup:
            event_type = "collected"
        else:
            event_type = "shipment_dispatched"
            if self.carrier_id:
                payload["carrier"] = self.carrier_id.name
            if self.carrier_tracking_ref:
                payload["tracking_number"] = self.carrier_tracking_ref
        self.env["backstage.outbox"].enqueue(
            order, event_type, "picking-%s-done" % self.id, payload, notification="send", record=self,
        )

    def _backstage_emit_return(self):
        """Devolució de client validada → return_received (Magento només ho registra; suppress)."""
        self.ensure_one()
        if self.picking_type_code != "incoming" or self.location_id.usage != "customer":
            return
        order = self.sale_id or self.move_ids.sale_line_id.order_id[:1]
        if not order or not order.magento_increment_id:
            return
        if not order._backstage_link_or_defer(self, "_backstage_emit_return"):
            return
        items, errors = self._backstage_shipment_items()
        if errors:
            return self._backstage_missing_items(order, errors)
        if not items:
            return
        source = self.env["backstage.source"].source_for_location(self.location_dest_id)
        payload = {"leg_ref": self.name, "items": items}
        if source:
            payload["destination_source_code"] = source.source_code
        self.env["backstage.outbox"].enqueue(
            order, "return_received", "picking-%s-done" % self.id, payload, notification="suppress", record=self,
        )

    def _backstage_emit_delivered(self):
        """El transportista ha lliurat l'enviament a client → delivered (només enviaments a domicili)."""
        self.ensure_one()
        if not self.env["backstage.backend"]._get().delivered_enabled:
            return
        order = self.sale_id
        if not order or not order.magento_increment_id or order.magento_is_pickup:
            return
        if self.state != "done" or self.picking_type_code != "outgoing" or self.location_dest_id.usage != "customer":
            return
        self.env["backstage.outbox"].enqueue(
            order, "delivered", "picking-%s-delivered" % self.id,
            {"leg_ref": self.name, "delivered_at": fields.Datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")},
            notification="send", record=self,
        )

    # ------------------------------------------------------------------ traspàs web: surt de l'origen

    def _backstage_quantities_done(self):
        quantities = defaultdict(float)
        for move in self.move_ids.filtered(lambda m: m.state == "done"):
            quantities[move.product_id] += move.quantity
        return quantities

    def _backstage_web_supply_departed(self):
        self.ensure_one()
        warehouse = self.location_dest_id.warehouse_id or self.env["stock.warehouse"].search(
            [("backstage_transit_location_id", "=", self.location_dest_id.id)], limit=1)
        orders = pickup_orders_for_moves(self.env, self.move_ids)
        quantities = self._backstage_quantities_done()
        if not quantities:
            return

        transit, stock = warehouse.backstage_transit_location_id, warehouse.lot_stock_id
        # Cada línia de la recepció queda enllaçada amb el lliurament de la seva comanda (com els passos d'una
        # ruta): en validar-la, Odoo reserva aquelles unitats per a aquesta comanda i cap altra no se les endú.
        pickups = self.env["stock.picking"]
        for order in orders:
            pickups |= order._backstage_pickup_picking()
        dest_moves = pickups.move_ids.filtered(lambda m: m.state not in ("done", "cancel"))
        receipt = self.env["stock.picking"].create({
            "picking_type_id": warehouse.backstage_receipt_type_id.id,
            "location_id": transit.id,
            "location_dest_id": stock.id,
            "origin": self.origin,
            "backstage_supply_picking_id": self.id,
            "move_ids": [(0, 0, {
                "product_id": product.id,
                "product_uom_qty": qty,
                "product_uom": product.uom_id.id,
                "location_id": transit.id,
                "location_dest_id": stock.id,
                "origin": self.origin,
                "move_dest_ids": [(6, 0, dest_moves.filtered(lambda m, p=product: m.product_id == p).ids)],
            }) for product, qty in quantities.items()],
        })
        if orders:
            receipt.sale_id = orders[:1]
        receipt.action_confirm()
        receipt.action_assign()

        Source = self.env["backstage.source"]
        origin_source = Source.source_for_location(self.location_id).source_code or ""
        destination_source = Source.source_for_location(stock).source_code or ""
        for order in orders:
            self.env["backstage.outbox"].enqueue(
                order, "in_transit_to_store", "picking-%s-transit" % self.id,
                {
                    "leg_ref": self.name,
                    "origin_source_code": origin_source,
                    "destination_source_code": destination_source,
                    "items": [{"sku": p.default_code or "", "qty": q} for p, q in quantities.items()],
                },
                notification="suppress", record=self,
            )

    # ------------------------------------------------------------------ traspàs web: la botiga rep

    def _backstage_receipt_expected(self):
        """El que encara ha d'arribar d'aquest traspàs: l'enviat menys el rebut en altres recepcions validades."""
        self.ensure_one()
        supply = self.backstage_supply_picking_id
        expected = supply._backstage_quantities_done()
        others = self.search([
            ("backstage_supply_picking_id", "=", supply.id), ("state", "=", "done"), ("id", "!=", self.id),
        ])
        for product, qty in others._backstage_quantities_done().items():
            expected[product] -= qty
        return expected

    def _backstage_receipt_planned(self):
        """Quantitats que la botiga està a punt de validar (les marcades, o totes si no n'hi ha cap)."""
        moves = self.move_ids.filtered(lambda m: m.state not in ("done", "cancel"))
        if any(moves.mapped("picked")):
            moves = moves.filtered("picked")
        quantities = defaultdict(float)
        for move in moves:
            quantities[move.product_id] += move.quantity
        return quantities

    def _backstage_receipt_differences(self, received):
        expected = self._backstage_receipt_expected()
        return [
            (product, expected.get(product, 0.0), received.get(product, 0.0))
            for product in sorted(set(expected) | set(received), key=lambda p: p.id)
            if abs(expected.get(product, 0.0) - received.get(product, 0.0)) > PRECISION
        ]

    def button_validate(self):
        """Recepció web amb diferències: abans de validar, la botiga n'indica el motiu."""
        if len(self) == 1 and self.backstage_supply_picking_id and self.state not in ("done", "cancel") \
                and not self.env.context.get(INCIDENTS_DECLARED_KEY):
            differences = self._backstage_receipt_differences(self._backstage_receipt_planned())
            if differences:
                wizard = self.env["backstage.web.receipt.incident.wizard"].create({
                    "picking_id": self.id,
                    "line_ids": [(0, 0, {
                        "product_id": product.id,
                        "qty_expected": expected,
                        "qty_received": received,
                        "reason": suggested_reason(expected, received),
                    }) for product, expected, received in differences],
                })
                return {
                    "type": "ir.actions.act_window",
                    "name": _("What arrived does not match what was sent"),
                    "res_model": wizard._name,
                    "res_id": wizard.id,
                    "view_mode": "form",
                    "target": "new",
                }
        return super().button_validate()

    def _backstage_record_incidents(self, differences, orders):
        """Confirma els motius declarats a l'assistent; les diferències sense motiu queden com a 'Sense motiu'."""
        Incident = self.env["backstage.web.receipt.incident"]
        drafts = self.backstage_incident_ids.filtered(lambda i: i.state == "draft")
        incidents = Incident
        for product, expected, received in differences:
            incident = drafts.filtered(lambda i: i.product_id == product)[:1]
            values = {"qty_expected": expected, "qty_received": received, "state": "open",
                      "sale_order_id": orders[:1].id}
            if incident:
                incident.write(values)
            else:
                incident = Incident.create(dict(values, picking_id=self.id, product_id=product.id))
            incidents |= incident
        (drafts - incidents).unlink()
        return incidents

    def _backstage_resolve_completed_incidents(self):
        """Faltants d'una recepció anterior que ja han arribat en un backorder: es tanquen sols."""
        supply = self.backstage_supply_picking_id
        receipts = self.search([("backstage_supply_picking_id", "=", supply.id), ("state", "=", "done")])
        sent, received = supply._backstage_quantities_done(), receipts._backstage_quantities_done()
        pending = self.env["backstage.web.receipt.incident"].search([
            ("supply_picking_id", "=", supply.id), ("state", "=", "open"),
            ("reason", "in", SELF_RESOLVING_REASONS), ("picking_id", "!=", self.id),
        ])
        done = pending.filtered(lambda i: abs(sent.get(i.product_id, 0.0) - received.get(i.product_id, 0.0)) <= PRECISION)
        if done:
            done.write({"resolution": _("Received in a later receipt (%s).", self.name)})
            done.action_resolve()

    def _backstage_web_receipt_validated(self):
        self.ensure_one()
        received = self._backstage_quantities_done()
        differences = self._backstage_receipt_differences(received)
        orders = pickup_orders_for_moves(self.env, self.move_ids) | self.sale_id.filtered("magento_is_pickup")
        incidents = self._backstage_record_incidents(differences, orders)
        reasons = dict(self.env["backstage.web.receipt.incident"]._fields["reason"]._description_selection(self.env))
        lines = [
            _("%(product)s: sent %(sent)s, received %(received)s — %(reason)s%(note)s",
              product=i.product_id.display_name, sent=i.qty_expected, received=i.qty_received,
              reason=reasons.get(i.reason, dict(REASONS).get(i.reason)), note=(" (%s)" % i.note) if i.note else "")
            for i in incidents
        ]
        self._backstage_resolve_completed_incidents()

        for order in orders:
            pickup = order._backstage_pickup_picking()
            if pickup:
                pickup.action_assign()
            if lines:
                order._backstage_notify(_("Web receipt incident %s", self.name), "<br/>".join(lines))
                order.message_post(body=_("Receipt %s has differences: the customer is not notified.", self.name))
                continue
            order.invalidate_recordset(["backstage_can_notify_ready"])
            if order.backstage_can_notify_ready:
                source = self.env["backstage.source"].source_for_location(pickup.location_id)
                self.env["backstage.outbox"].enqueue(
                    order, "ready_for_pickup", "so-%s-ready" % order.id,
                    {"pickup_location_code": source.source_code or "", "leg_ref": self.name},
                    notification="send", record=self,
                )
            elif pickup and not order._backstage_pending_supply():
                self._backstage_notify_not_reserved(order, pickup)

    def _backstage_notify_not_reserved(self, order, pickup):
        """Ha arribat tot, però el lliurament no queda reservat del tot (p. ex. una altra comanda ha reservat part
        del material, o la recepció no estava enllaçada): no s'avisa el client i la botiga ha de saber per què."""
        lines = []
        for move in pickup.move_ids.filtered(lambda m: m.state not in ("done", "cancel", "assigned")):
            holders = self.env["stock.move.line"].search([
                ("product_id", "=", move.product_id.id), ("location_id", "child_of", pickup.location_id.id),
                ("state", "not in", ("done", "cancel")), ("picking_id", "!=", pickup.id),
            ]).picking_id
            lines.append(_(
                "%(product)s: %(missing)s missing; reserved for %(holders)s",
                product=move.product_id.display_name, missing=move.product_uom_qty - move.quantity,
                holders=", ".join("%s (%s)" % (p.name, p.origin or "-") for p in holders) or _("no other order"),
            ))
        order._backstage_notify(
            _("Ready for pickup on hold: %s", order.name),
            _("All the goods of %(receipt)s arrived, but the pickup transfer %(pickup)s is not fully reserved, "
              "so the customer was not told yet.<br/>%(lines)s<br/>Free those reservations or wait for more stock, "
              "then press Ready for pickup.", receipt=self.name, pickup=pickup.name, lines="<br/>".join(lines)),
        )
