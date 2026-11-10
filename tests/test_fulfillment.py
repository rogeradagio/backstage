"""comprova la cua d’enviament d’esdeveniments i els seus disparadors principals."""

import json
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.tests.common import trap_jobs

from ..models.backstage_client import MagentoResponse
from .common import setup_backend



@tagged("post_install", "-at_install")
class TestBackstage(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backend = setup_backend(cls.env)

        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.env.company.id)], limit=1)
        cls.stock_location = cls.warehouse.lot_stock_id
        Source = cls.env["backstage.source"]
        mapping = Source.with_context(active_test=False).search([("location_id", "=", cls.stock_location.id)])
        if mapping:
            mapping.write({"source_code": "store_01", "active": True})
        else:
            Source.create({"location_id": cls.stock_location.id, "source_code": "store_01"})
        cls.product = cls.env["product.product"].create({
            "name": "Micròfon de prova", "default_code": "013634", "type": "consu", "is_storable": True, "list_price": 100,
        })
        cls.env["stock.quant"]._update_available_quantity(cls.product, cls.stock_location, 10)
        cls.partner = cls.env["res.partner"].create({"name": "Client Prova"})

    def _order(self, pickup=True, increment="ES000099001"):
        order = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "warehouse_id": self.warehouse.id,
            "magento_increment_id": increment,
            "magento_store_code": "es_es",
            "magento_shipping_method": "instore_pickup" if pickup else "tablerate_bestway",
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1, "magento_order_item_id": 4338})],
        })
        order.action_confirm()
        return order

    def _deliver(self, order):
        picking = order.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing")
        picking.action_assign()
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        picking._action_done()
        return picking

    def _patch_client(self, status, body):
        return patch.object(
            type(self.env["backstage.client"]), "post_event",
            lambda _self, envelope: MagentoResponse(status, body),
        )

    # ------------------------------------------------------------------ cua de sortida

    def test_disabled_creates_nothing(self):
        self.backend.enabled = False
        order = self._order()
        outbox = self.env["backstage.outbox"].enqueue(order, "ready_for_pickup", "x-1", {})
        self.assertFalse(outbox)

    def test_enqueue_is_idempotent_and_frozen(self):
        order = self._order()
        with trap_jobs() as trap:
            first = self.env["backstage.outbox"].enqueue(order, "ready_for_pickup", "so-test-ready", {"pickup_location_code": "store_01"}, notification="send")
            second = self.env["backstage.outbox"].enqueue(order, "ready_for_pickup", "so-test-ready", {"pickup_location_code": "store_02"}, notification="send")
            trap.assert_jobs_count(1, only=first.send_to_magento)
        self.assertEqual(first, second)
        envelope = json.loads(first.envelope)
        self.assertEqual(envelope["order_reference"], "ES000099001")
        self.assertEqual(json.loads(envelope["payload"]), {"pickup_location_code": "store_01"})

    def test_suppress_context_forces_suppress(self):
        order = self._order()
        with trap_jobs():
            outbox = self.env["backstage.outbox"].with_context(backstage_suppress=True).enqueue(
                order, "ready_for_pickup", "so-test-sup", {}, notification="send")
        self.assertEqual(outbox.notification_mode, "suppress")
        self.assertEqual(json.loads(outbox.envelope)["notification"]["mode"], "suppress")

    def test_send_ok_rejected_and_retry(self):
        order = self._order()
        with trap_jobs():
            ok = self.env["backstage.outbox"].enqueue(order, "ready_for_pickup", "so-ok", {})
            rejected = self.env["backstage.outbox"].enqueue(order, "delivered", "so-rej", {"leg_ref": "X"})
            failing = self.env["backstage.outbox"].enqueue(order, "ready_for_pickup", "so-fail", {})

        with self._patch_client(200, {"status": "processed", "magento_shipment_ids": [7]}):
            ok.send_to_magento()
        self.assertEqual(ok.state, "sent")
        self.assertEqual(json.loads(ok.magento_document_ids), {"magento_shipment_ids": [7]})

        activities_before = len(order.activity_ids)
        with self._patch_client(422, {"message": "[transition_not_allowed] nope"}):
            rejected.send_to_magento()
        self.assertEqual(rejected.state, "rejected")
        self.assertEqual(rejected.error_code, "transition_not_allowed")
        self.assertEqual(len(order.activity_ids), activities_before + 1)

        # Sense assertRaises: Odoo reverteix fins a un punt de desament i esborraria l'estat escrit.
        raised = False
        with self._patch_client(503, {"message": "[temporary_failure] retry"}):
            try:
                failing.send_to_magento()
            except RetryableJobError:
                raised = True
        self.assertTrue(raised)
        self.assertEqual(failing.state, "failed")
        self.assertEqual(failing.http_status, 503)

    # ------------------------------------------------------------------ disparadors

    def test_pickup_delivery_emits_collected(self):
        order = self._order(pickup=True)
        with trap_jobs():
            picking = self._deliver(order)
        outbox = order.backstage_outbox_ids.filtered(lambda o: o.event_type == "collected")
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox.event_id, "picking-%s-done" % picking.id)
        payload = json.loads(json.loads(outbox.envelope)["payload"])
        self.assertEqual(payload["source_code"], "store_01")
        self.assertEqual(payload["items"], [{"order_item_id": 4338, "sku": "013634", "qty": 1.0}])

    def test_home_delivery_emits_shipment_dispatched(self):
        order = self._order(pickup=False, increment="ES000099002")
        with trap_jobs():
            self._deliver(order)
        self.assertEqual(order.backstage_outbox_ids.mapped("event_type"), ["shipment_dispatched"])

    def test_ready_button(self):
        order = self._order()
        picking = order.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing")
        picking.action_assign()
        order.invalidate_recordset(["backstage_can_notify_ready"])
        self.assertTrue(order.backstage_can_notify_ready)
        with trap_jobs():
            order.action_backstage_ready_for_pickup()
            order.action_backstage_ready_for_pickup()  # repetit: mateix event_id, cap duplicat
        ready = order.backstage_outbox_ids.filtered(lambda o: o.event_type == "ready_for_pickup")
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready.notification_mode, "send")

    def test_reposted_corrected_invoice_emits_new_event(self):
        order = self._order(increment="ES000099004")
        with trap_jobs():
            self._deliver(order)
            invoice = order._create_invoices()
            invoice.action_post()
            invoice.button_draft()
            invoice.action_post()  # republicada igual: mateix esdeveniment
            invoice.button_draft()
            invoice.invoice_line_ids.write({"price_unit": 90})
            invoice.action_post()  # republicada corregida: esdeveniment nou
        events = order.backstage_outbox_ids.filtered(lambda o: o.event_type == "invoice_issued")
        self.assertEqual(len(events), 2)

    def test_invoice_emits_invoice_issued_with_suppress(self):
        order = self._order()
        with trap_jobs():
            self._deliver(order)
            invoice = order._create_invoices()
            invoice.action_post()
        outbox = order.backstage_outbox_ids.filtered(lambda o: o.event_type == "invoice_issued")
        self.assertEqual(len(outbox), 1)
        envelope = json.loads(outbox.envelope)
        self.assertEqual(envelope["notification"]["mode"], "suppress")
        payload = json.loads(envelope["payload"])
        self.assertEqual(payload["odoo_invoice_id"], invoice.id)
        self.assertEqual(payload["grand_total"], round(invoice.amount_total, 2))
        self.assertEqual(payload["items"][0]["order_item_id"], 4338)

    def test_cancel_emits_cancelled(self):
        order = self._order(increment="ES000099003")
        with trap_jobs():
            order._action_cancel()
        self.assertEqual(order.backstage_outbox_ids.mapped("event_type"), ["cancelled"])
