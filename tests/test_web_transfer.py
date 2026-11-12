"""comprova els traspassos web, les recepcions i les incidències de botiga."""

import json

from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.queue_job.tests.common import trap_jobs

from .common import setup_backend



@tagged("post_install", "-at_install")
class TestWebTransfer(TransactionCase):
    """§5.3: traspassos que alimenten una comanda web de recollida, en 2 passos amb recepció a la botiga."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backend = setup_backend(cls.env)
        cls.central = cls.env["stock.warehouse"].search([("company_id", "=", cls.env.company.id)], limit=1)
        cls.store = cls.env["stock.warehouse"].create({"name": "Botiga Test", "code": "TBT", "company_id": cls.env.company.id})
        cls.store.action_backstage_setup_pickup()
        Source = cls.env["backstage.source"]
        for warehouse, code in ((cls.central, "erp"), (cls.store, "botiga_test")):
            mapping = Source.with_context(active_test=False).search([("location_id", "=", warehouse.lot_stock_id.id)])
            if mapping:
                mapping.write({"source_code": code, "active": True})
            else:
                Source.create({"location_id": warehouse.lot_stock_id.id, "source_code": code})
        cls.product = cls.env["product.product"].create({
            "name": "Guitarra web", "default_code": "GTR-1", "type": "consu", "is_storable": True,
        })
        cls.env["stock.quant"]._update_available_quantity(cls.product, cls.central.lot_stock_id, 10)
        cls.partner = cls.env["res.partner"].create({"name": "Client recollida"})

    def _pickup_order(self, qty=2):
        order = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "warehouse_id": self.store.id,
            "magento_increment_id": "ES-WEB-%s" % qty,
            "magento_store_code": "es_es",
            "magento_shipping_method": "instore_pickup",
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": qty, "magento_order_item_id": 9})],
        })
        order.action_confirm()
        return order

    def _supply(self, order, qty, origin_name=None):
        """Simula el traspàs del magatzem central a la botiga (origin = comanda)."""
        picking = self.env["stock.picking"].create({
            "picking_type_id": self.central.int_type_id.id,
            "location_id": self.central.lot_stock_id.id,
            "location_dest_id": self.store.lot_stock_id.id,
            "origin": origin_name or order.name,
            "move_ids": [(0, 0, {
                "product_id": self.product.id, "product_uom_qty": qty, "product_uom": self.product.uom_id.id,
                "location_id": self.central.lot_stock_id.id, "location_dest_id": self.store.lot_stock_id.id,
                "origin": origin_name or order.name,
            })],
        })
        picking.action_confirm()
        picking.action_assign()
        return picking

    def _validate(self, picking, qty=None):
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty if qty is None else qty
            move.picked = True
        picking._action_done()

    def test_web_supply_goes_through_transit_and_store_receipt(self):
        order = self._pickup_order(2)
        supply = self._supply(order, 2)
        self.assertEqual(supply.location_dest_id, self.store.backstage_transit_location_id)
        self.assertTrue(supply.backstage_web_supply)
        self.assertFalse(order.backstage_can_notify_ready)

        with trap_jobs():
            self._validate(supply)
        receipt = self.env["stock.picking"].search([("backstage_supply_picking_id", "=", supply.id)])
        self.assertEqual(receipt.picking_type_id, self.store.backstage_receipt_type_id)
        self.assertEqual(receipt.state, "assigned")
        transit_event = order.backstage_outbox_ids.filtered(lambda o: o.event_type == "in_transit_to_store")
        payload = json.loads(json.loads(transit_event.envelope)["payload"])
        self.assertEqual((payload["origin_source_code"], payload["destination_source_code"]), ("erp", "botiga_test"))
        self.assertEqual(transit_event.notification_mode, "suppress")

        # Mentre viatja: no és a l'estoc de la botiga i no es pot avisar el client
        store_qty = self.product.with_context(location=self.store.lot_stock_id.id).qty_available
        self.assertEqual(store_qty, 0)
        order.invalidate_recordset(["backstage_can_notify_ready"])
        self.assertFalse(order.backstage_can_notify_ready)

        with trap_jobs():
            self._validate(receipt)
        ready = order.backstage_outbox_ids.filtered(lambda o: o.event_type == "ready_for_pickup")
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready.notification_mode, "send")
        self.assertEqual(order._backstage_pickup_picking().state, "assigned")

    def test_stock_export_counts_web_supply_once(self):
        """Magento ha de veure sempre el mateix venible total (5 lliures de 10, menys la reserva MSI d'1)."""
        self.env["backstage.source"].search([("location_id", "in", (self.central.lot_stock_id | self.store.lot_stock_id).ids)]).write({"export_stock": True})
        Stock = self.env["backstage.stock"]
        sources = Stock._export_sources().filtered(lambda s: s.source_code in ("erp", "botiga_test"))

        def sellable():
            values = Stock.compute_sellable(self.product, sources)
            return values[(self.product.id, "erp")], values[(self.product.id, "botiga_test")]

        order = self._pickup_order(1)
        supply = self._supply(order, 1)
        self.assertEqual(sellable(), (10, 0))   # traspàs web reservat al magatzem central: no es resta
        with trap_jobs():
            self._validate(supply)
        self.assertEqual(sellable(), (9, 1))    # en trànsit: compta a la font de la botiga
        receipt = self.env["stock.picking"].search([("backstage_supply_picking_id", "=", supply.id)])
        with trap_jobs():
            self._validate(receipt)
        self.assertEqual(sellable(), (9, 1))    # rebut i reservat per a la recollida web: no es resta

    def test_short_receipt_creates_incident_and_no_ready(self):
        order = self._pickup_order(3)
        supply = self._supply(order, 3)
        with trap_jobs():
            self._validate(supply)
        receipt = self.env["stock.picking"].search([("backstage_supply_picking_id", "=", supply.id)])
        activities_before = len(order.activity_ids)
        with trap_jobs():
            self._validate(receipt, qty=2)  # n'arriben 2 de 3
        self.assertFalse(order.backstage_outbox_ids.filtered(lambda o: o.event_type == "ready_for_pickup"))
        self.assertEqual(len(order.activity_ids), activities_before + 1)
        self.assertIn("incident", order.activity_ids[-1].summary)

    def test_regular_replenishment_is_not_rerouted(self):
        supply = self.env["stock.picking"].create({
            "picking_type_id": self.central.int_type_id.id,
            "location_id": self.central.lot_stock_id.id,
            "location_dest_id": self.store.lot_stock_id.id,
            "origin": "Reposició botiga",
            "move_ids": [(0, 0, {
                "product_id": self.product.id, "product_uom_qty": 1, "product_uom": self.product.uom_id.id,
                "location_id": self.central.lot_stock_id.id, "location_dest_id": self.store.lot_stock_id.id,
            })],
        })
        supply.action_confirm()
        self.assertEqual(supply.location_dest_id, self.store.lot_stock_id)
        self.assertFalse(supply.backstage_web_supply)

    # ------------------------------------------------------------------ incidències de recepció

    def _departed_receipt(self, order, qty):
        supply = self._supply(order, qty)
        with trap_jobs():
            self._validate(supply)
        return supply, self.env["stock.picking"].search([("backstage_supply_picking_id", "=", supply.id)])

    # ------------------------------------------------------------------ reserva per a la comanda del traspàs

    def _ready_events(self, order):
        return order.backstage_outbox_ids.filtered(lambda o: o.event_type == "ready_for_pickup")

    def test_receipt_is_reserved_for_its_own_order(self):
        """Cas real: una comanda amb una incidència pendent (n'espera 1) no s'endú el material d'una altra."""
        waiting = self._pickup_order(3)
        _supply, receipt = self._departed_receipt(waiting, 3)
        self.assertTrue(receipt.move_ids.move_dest_ids)          # enllaçada amb el lliurament de la comanda
        with trap_jobs():
            self._validate(receipt, qty=2)                       # en falta 1: queda esperant
        self.assertFalse(self._ready_events(waiting))

        order = self._pickup_order(2)
        _supply, receipt = self._departed_receipt(order, 2)
        with trap_jobs():
            self._validate(receipt)
        pickup = order._backstage_pickup_picking()
        self.assertEqual(pickup.move_ids.mapped("state"), ["assigned"])
        self.assertEqual(pickup.move_ids.quantity, 2)
        self.assertEqual(len(self._ready_events(order)), 1)
        self.assertEqual(waiting._backstage_pickup_picking().move_ids.quantity, 2)  # continua esperant la seva

    def test_partially_reserved_pickup_is_not_ready(self):
        """Xarxa de seguretat: la recepció arriba sencera però el lliurament no queda reservat del tot
        (aquí, una segona línia que no venia amb el traspàs): no s'avisa el client i s'explica per què."""
        other = self.env["product.product"].create({"name": "Cable", "default_code": "CBL-1", "type": "consu", "is_storable": True})
        order = self._pickup_order(2)
        order.order_line = [(0, 0, {"product_id": other.id, "product_uom_qty": 1, "magento_order_item_id": 10})]
        _supply, receipt = self._departed_receipt(order, 2)
        activities = len(order.activity_ids)
        with trap_jobs():
            self._validate(receipt)
        order.invalidate_recordset(["backstage_can_notify_ready"])
        self.assertEqual(order._backstage_pickup_picking().state, "assigned")   # "Llest", però parcial
        self.assertFalse(order.backstage_can_notify_ready)
        self.assertFalse(self._ready_events(order))
        self.assertEqual(len(order.activity_ids), activities + 1)
        self.assertIn("CBL-1", order.activity_ids[-1].note)

    def test_short_receipt_without_wizard_is_unspecified(self):
        order = self._pickup_order(3)
        _supply, receipt = self._departed_receipt(order, 3)
        with trap_jobs():
            self._validate(receipt, qty=2)
        incident = receipt.backstage_incident_ids
        self.assertEqual(len(incident), 1)
        self.assertEqual((incident.state, incident.reason, incident.qty_expected, incident.qty_received),
                         ("open", "unspecified", 3, 2))
        self.assertEqual(incident.sale_order_id, order)

        # Els faltants sense motiu es tanquen sols quan arriben a la comanda pendent.
        backorder = self.env["stock.picking"].search([("backorder_id", "=", receipt.id)])
        with trap_jobs():
            self._validate(backorder)
        self.assertEqual(incident.state, "resolved")
        self.assertIn(backorder.name, incident.resolution)

    def test_full_receipt_skips_wizard(self):
        order = self._pickup_order(1)
        _supply, receipt = self._departed_receipt(order, 1)
        with trap_jobs():
            result = receipt.button_validate()
        self.assertIs(result, True)
        self.assertEqual(receipt.state, "done")
        self.assertFalse(receipt.backstage_incident_ids)

    def test_wizard_reason_backorder_and_auto_resolve(self):
        order = self._pickup_order(4)
        supply, receipt = self._departed_receipt(order, 4)
        receipt.move_ids.quantity = 3
        receipt.move_ids.picked = True

        action = receipt.button_validate()
        self.assertEqual(action["res_model"], "backstage.web.receipt.incident.wizard")
        wizard = self.env[action["res_model"]].browse(action["res_id"])
        self.assertEqual(wizard.line_ids.reason, "short")
        self.assertEqual((wizard.line_ids.qty_expected, wizard.line_ids.qty_received), (4, 3))
        wizard.line_ids.write({"reason": "damaged", "note": "Caixa trencada"})

        with trap_jobs():
            backorder_action = wizard.action_confirm()
            self.assertEqual(backorder_action["res_model"], "stock.backorder.confirmation")
            Form = self.env["stock.backorder.confirmation"].with_context(backorder_action["context"])
            Form.create({"pick_ids": [(6, 0, receipt.ids)]}).process()
        self.assertEqual(receipt.state, "done")
        incident = receipt.backstage_incident_ids
        self.assertEqual((incident.state, incident.reason, incident.note), ("open", "damaged", "Caixa trencada"))
        self.assertIn("Damaged", order.activity_ids[-1].note)
        self.assertFalse(order.backstage_outbox_ids.filtered(lambda o: o.event_type == "ready_for_pickup"))

        # La peça arriba a la comanda pendent: s'avisa el client, però la malmesa queda oberta per al magatzem central.
        backorder = self.env["stock.picking"].search([("backorder_id", "=", receipt.id)])
        self.assertEqual(backorder.backstage_supply_picking_id, supply)
        with trap_jobs():
            self.assertIs(backorder.button_validate(), True)
        self.assertFalse(backorder.backstage_incident_ids)
        self.assertEqual(incident.state, "open")
        self.assertEqual(len(order.backstage_outbox_ids.filtered(lambda o: o.event_type == "ready_for_pickup")), 1)

    def test_store_can_declare_twice(self):
        """La botiga cancel·la el backorder i torna a validar: els esborranys anteriors s'han de poder substituir."""
        store_user = new_test_user(self.env, login="bk_store_twice", groups="stock.group_stock_user")
        order = self._pickup_order(5)
        _supply, receipt = self._departed_receipt(order, 5)
        receipt.move_ids.write({"quantity": 4, "picked": True})
        receipt = receipt.with_user(store_user)
        for reason in ("damaged", "wrong_item"):
            action = receipt.button_validate()
            wizard = self.env[action["res_model"]].with_user(store_user).browse(action["res_id"])
            wizard.line_ids.reason = reason
            backorder_action = wizard.action_confirm()   # la botiga tanca el diàleg del backorder sense triar
            self.assertEqual(backorder_action["res_model"], "stock.backorder.confirmation")
        drafts = receipt.sudo().backstage_incident_ids
        self.assertEqual((drafts.state, drafts.reason), ("draft", "wrong_item"))
        self.assertFalse(drafts.with_user(store_user).can_resolve)

    def test_unexpected_product_is_declared(self):
        other = self.env["product.product"].create({"name": "Pua", "default_code": "PUA-1", "type": "consu", "is_storable": True})
        order = self._pickup_order(1)
        _supply, receipt = self._departed_receipt(order, 1)
        self.env["stock.move"].create({
            "picking_id": receipt.id, "product_id": other.id, "product_uom_qty": 1, "product_uom": other.uom_id.id,
            "location_id": receipt.location_id.id, "location_dest_id": receipt.location_dest_id.id, "quantity": 1,
        })
        action = receipt.button_validate()
        wizard = self.env[action["res_model"]].browse(action["res_id"])
        line = wizard.line_ids.filtered(lambda l: l.product_id == other)
        self.assertEqual((line.reason, line.qty_expected, line.qty_received), ("unexpected", 0, 1))
