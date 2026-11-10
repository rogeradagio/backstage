"""comprova l’emissió dels esdeveniments de preparació, devolució i lliurament."""

import json

from odoo.tests import TransactionCase, tagged

from odoo.addons.queue_job.tests.common import trap_jobs

from .common import setup_backend



@tagged("post_install", "-at_install")
class TestMagentoEvents(TransactionCase):
    """preparing_at_warehouse, return_received i delivered."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backend = setup_backend(cls.env)

        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.env.company.id)], limit=1)
        cls.stock_location = cls.warehouse.lot_stock_id
        Source = cls.env["backstage.source"]
        mapping = Source.with_context(active_test=False).search([("location_id", "=", cls.stock_location.id)])
        if mapping:
            mapping.write({"source_code": "erp", "active": True})
        else:
            Source.create({"location_id": cls.stock_location.id, "source_code": "erp"})
        cls.product = cls.env["product.product"].create({
            "name": "Teclat de prova", "default_code": "013636", "type": "consu", "is_storable": True, "list_price": 100,
        })
        cls.env["stock.quant"]._update_available_quantity(cls.product, cls.stock_location, 10)
        cls.partner = cls.env["res.partner"].create({"name": "Client Esdeveniments"})
        cls.substate = cls.env["base.substate"].create({
            "name": "Warehouse",
            "target_state_value_id": cls.env.ref("sale_substate.target_state_value_sale").id,
        })
        cls.backend.preparing_substate_id = cls.substate

    def _order(self, pickup=False, increment="ES000077001"):
        order = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "warehouse_id": self.warehouse.id,
            "magento_increment_id": increment,
            "magento_store_code": "es_es",
            "magento_shipping_method": "instore_pickup" if pickup else "tablerate_bestway",
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 2, "magento_order_item_id": 5501})],
        })
        order.action_confirm()
        return order

    def _validate(self, picking, qty=None):
        picking.action_assign()
        for move in picking.move_ids:
            move.quantity = qty if qty is not None else move.product_uom_qty
            move.picked = True
        picking._action_done()
        return picking

    def _deliver(self, order):
        return self._validate(order.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing"))

    def _outbox(self, order, event_type):
        return self.env["backstage.outbox"].search([
            ("sale_order_id", "=", order.id), ("event_type", "=", event_type),
        ])

    # ------------------------------------------------------------------ esdeveniment preparing_at_warehouse

    def test_preparing_on_substate(self):
        order = self._order()
        with trap_jobs():
            order.substate_id = self.substate
            order.substate_id = self.substate  # idempotent
        outbox = self._outbox(order, "preparing_at_warehouse")
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox.event_id, "so-%s-preparing" % order.id)
        envelope = json.loads(outbox.envelope)
        self.assertEqual(json.loads(envelope["payload"]), {"origin_source_codes": ["erp"]})
        self.assertEqual(envelope["notification"]["mode"], "send")

    def test_other_substate_emits_nothing(self):
        other = self.env["base.substate"].create({
            "name": "Altres", "target_state_value_id": self.env.ref("sale_substate.target_state_value_sale").id,
        })
        order = self._order()
        with trap_jobs():
            order.substate_id = other
        self.assertFalse(self._outbox(order, "preparing_at_warehouse"))

    # ------------------------------------------------------------------ esdeveniment return_received

    def _return(self, picking, qty):
        wizard = self.env["stock.return.picking"].with_context(active_id=picking.id, active_model="stock.picking").create({})
        wizard.product_return_moves.quantity = qty
        action = wizard.action_create_returns()
        return self.env["stock.picking"].browse(action["res_id"])

    def test_return_received(self):
        order = self._order()
        with trap_jobs():
            delivery = self._deliver(order)
            returned = self._return(delivery, 1)
            self._validate(returned, 1)
        outbox = self._outbox(order, "return_received")
        self.assertEqual(len(outbox), 1)
        envelope = json.loads(outbox.envelope)
        payload = json.loads(envelope["payload"])
        self.assertEqual(payload["leg_ref"], returned.name)
        self.assertEqual(payload["destination_source_code"], "erp")
        self.assertEqual(payload["items"], [{"order_item_id": 5501, "sku": "013636", "qty": 1.0}])
        self.assertEqual(envelope["notification"]["mode"], "suppress")
        self.assertEqual(outbox.event_id, "picking-%s-done" % returned.id)

    # ------------------------------------------------------------------ esdeveniment delivered

    def test_delivered_disabled_by_default(self):
        order = self._order()
        with trap_jobs():
            delivery = self._deliver(order)
            delivery._backstage_emit_delivered()
        self.assertFalse(self._outbox(order, "delivered"))

    def test_delivered_when_enabled(self):
        self.backend.delivered_enabled = True
        order = self._order()
        with trap_jobs():
            delivery = self._deliver(order)
            delivery._backstage_emit_delivered()
            delivery._backstage_emit_delivered()
        outbox = self._outbox(order, "delivered")
        self.assertEqual(len(outbox), 1)
        payload = json.loads(json.loads(outbox.envelope)["payload"])
        self.assertEqual(payload["leg_ref"], delivery.name)
        self.assertIn("delivered_at", payload)

    def test_delivered_never_for_pickup(self):
        self.backend.delivered_enabled = True
        order = self._order(pickup=True, increment="ES000077002")
        with trap_jobs():
            delivery = self._deliver(order)
            delivery._backstage_emit_delivered()
        self.assertFalse(self._outbox(order, "delivered"))
