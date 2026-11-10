"""comprova la connexió, els estats de Backstage i les accions de la llista Per revisar."""

import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.queue_job.tests.common import trap_jobs

from ..models.backstage_client import MagentoResponse
from ..models.backstage_outbox import REVIEW_DOMAIN
from .common import setup_backend

STORE_VIEWS = [{"id": 0, "code": "admin"}, {"id": 1, "code": "es_es", "name": "Español"}]


@tagged("post_install", "-at_install")
class TestBackstageBackend(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backend = setup_backend(cls.env)
        cls.Client = type(cls.env["backstage.client"])
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.env.company.id)], limit=1)
        cls.product = cls.env["product.product"].create({
            "name": "Bateria de prova", "default_code": "BKD-1", "type": "consu", "is_storable": True,
        })
        cls.env["stock.quant"]._update_available_quantity(cls.product, cls.warehouse.lot_stock_id, 4)
        cls.partner = cls.env["res.partner"].create({"name": "Client Backend"})

    def _order(self, increment="ES000066001"):
        order = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "warehouse_id": self.warehouse.id,
            "magento_increment_id": increment,
            "magento_store_code": "es_es",
            "magento_shipping_method": "tablerate_bestway",
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1, "magento_order_item_id": 1})],
        })
        order.action_confirm()
        return order

    # ------------------------------------------------------------------ connexió

    def test_connection_syncs_store_views_and_proposes_locations(self):
        sources = {"items": [{"source_code": self.warehouse.code.lower(), "name": "Central"},
                             {"source_code": "supplier_x", "name": "Proveïdor X"}]}
        self.env["backstage.source"].with_context(active_test=False).search(
            [("location_id", "=", self.warehouse.lot_stock_id.id)]).unlink()
        with patch.object(self.Client, "get_store_views", lambda _s: MagentoResponse(200, STORE_VIEWS)), \
                patch.object(self.Client, "get_sources", lambda _s: MagentoResponse(200, sources)):
            self.backend.action_test_connection()
        self.assertEqual(self.backend.connection_state, "ok")
        self.assertEqual(self.backend.store_view_ids.mapped("code"), ["es_es"])
        by_code = {s.source_code: s for s in self.backend.source_ids}
        self.assertEqual(by_code[self.warehouse.code.lower()].location_id, self.warehouse.lot_stock_id)
        self.assertFalse(by_code["supplier_x"].location_id)
        self.assertGreaterEqual(self.backend.unmapped_source_count, 1)

    def test_connection_explains_bad_token(self):
        with patch.object(self.Client, "get_store_views",
                          lambda _s: MagentoResponse(401, {"message": "The consumer isn't authorized"})):
            self.backend.action_test_connection()
        self.assertEqual(self.backend.connection_state, "error")
        self.assertIn("token", self.backend.connection_message)

    def test_switched_off_sends_nothing(self):
        self.backend.enabled = False
        order = self._order()
        self.assertFalse(self.env["backstage.outbox"].enqueue(order, "cancelled", "bk-off", {}, notification="send"))

    def test_switches_are_independent(self):
        self.backend.write({"enabled": False, "silent": True, "connection_state": "unknown"})
        with self.assertRaises(UserError):
            self.backend.action_enable()           # cal haver provat la connexió
        self.backend.connection_state = "ok"
        self.backend.action_enable()
        self.assertEqual((self.backend.enabled, self.backend.silent), (True, True))
        self.backend.action_notify_customers()
        order = self._order()
        with trap_jobs():
            outbox = self.env["backstage.outbox"].enqueue(order, "cancelled", "bk-notify", {}, notification="send")
        self.assertEqual(outbox.notification_mode, "send")
        self.backend.action_silent()
        self.backend.action_disable()
        self.assertEqual((self.backend.enabled, self.backend.silent), (False, True))

    def test_silent_mode_never_notifies_customers(self):
        self.backend.silent = True
        order = self._order()
        with trap_jobs():
            outbox = self.env["backstage.outbox"].enqueue(order, "cancelled", "bk-test", {}, notification="send")
        self.assertEqual(outbox.notification_mode, "suppress")
        self.assertEqual(json.loads(outbox.envelope)["notification"]["mode"], "suppress")

    # ------------------------------------------------------------------ per revisar

    def _rejected(self, order, event_id="bk-rej"):
        with trap_jobs():
            outbox = self.env["backstage.outbox"].enqueue(order, "cancelled", event_id, {}, notification="send")
        with patch.object(self.Client, "post_event", lambda _s, _e: MagentoResponse(
                422, {"message": "[transition_not_allowed] Order %1 is complete", "parameters": ["ES000066001"]})):
            outbox.send_to_magento()
        return outbox

    def test_rejection_is_explained_and_listed(self):
        order = self._order()
        outbox = self._rejected(order)
        self.assertIn(outbox, self.env["backstage.outbox"].search(REVIEW_DOMAIN))
        self.assertTrue(outbox.problem and outbox.next_step)
        self.assertTrue(outbox.activity_id)
        self.assertEqual(order.backstage_review_count, 1)
        self.assertEqual(self.backend.review_count, len(self.env["backstage.outbox"].search(REVIEW_DOMAIN)))

    def test_unknown_rejection_shows_magento_message(self):
        order = self._order()
        with trap_jobs():
            outbox = self.env["backstage.outbox"].enqueue(order, "cancelled", "bk-msg", {}, notification="send")
        with patch.object(self.Client, "post_event", lambda _s, _e: MagentoResponse(
                400, {"message": "[weird] Item %1 not found", "parameters": ["4338"]})):
            outbox.send_to_magento()
        self.assertIn("Item 4338 not found", outbox.problem)

    def test_cancel_of_shipped_order_suggests_return(self):
        order = self._order()
        with trap_jobs():
            outbox = self.env["backstage.outbox"].enqueue(order, "cancelled", "bk-shipped", {}, notification="send")
        with patch.object(self.Client, "post_event", lambda _s, _e: MagentoResponse(
                422, {"message": "[has_shipments] The order has shipments"})):
            outbox.send_to_magento()
        self.assertIn("return", outbox.next_step)

    def test_discard_closes_activity_and_leaves_review(self):
        order = self._order()
        outbox = self._rejected(order)
        activity = outbox.activity_id
        outbox.action_discard()
        self.assertEqual(outbox.state, "discarded")
        self.assertNotIn(outbox, self.env["backstage.outbox"].search(REVIEW_DOMAIN))
        self.assertFalse(activity.exists() and activity.active)

    def test_retry_requeues(self):
        order = self._order()
        outbox = self._rejected(order)
        with trap_jobs() as trap:
            outbox.action_retry()
            trap.assert_jobs_count(1, only=outbox.send_to_magento)
        self.assertEqual(outbox.state, "pending")

    def test_long_failure_needs_review(self):
        order = self._order()
        with trap_jobs():
            outbox = self.env["backstage.outbox"].enqueue(order, "cancelled", "bk-fail", {}, notification="send")
        outbox.write({"state": "failed", "attempts": 1})
        self.assertNotIn(outbox, self.env["backstage.outbox"].search(REVIEW_DOMAIN))
        outbox.attempts = 5
        self.assertIn(outbox, self.env["backstage.outbox"].search(REVIEW_DOMAIN))

    # ------------------------------------------------------------------ estoc a Magento

    def test_stock_preview_explains_quantity(self):
        self.backend.stock_enabled = True
        Source = self.env["backstage.source"].with_context(active_test=False)
        source = Source.search([("location_id", "=", self.warehouse.lot_stock_id.id)])
        if source:
            source.write({"export_stock": True, "active": True})
        else:
            source = Source.create({"location_id": self.warehouse.lot_stock_id.id, "source_code": "bk_src", "export_stock": True})
        preview = self.env["backstage.stock.preview"].create({"product_tmpl_id": self.product.product_tmpl_id.id})
        line = preview.line_ids.filtered(lambda l: l.source_id == source)
        self.assertEqual((line.on_hand, line.reserved_other, line.sellable), (4, 0, 4))
        self.assertTrue(self.product.product_tmpl_id.backstage_stock_visible)
