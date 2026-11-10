"""comprova la vinculació entre comandes d’Odoo i comandes de Magento."""

import json
from contextlib import contextmanager
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.tests.common import trap_jobs

from ..models.backstage_client import MagentoResponse
from ..models.backstage_outbox import REVIEW_DOMAIN
from .common import setup_backend

STORE_VIEWS = [{"id": 0, "code": "admin"}, {"id": 1, "code": "es_es"}, {"id": 4, "code": "pt_pt"}]


def magento_order(entity_id=7001, store_id=1, sku="013635", method="instore_pickup"):
    """Comanda de Magento amb un configurable: el pare i el fill porten el mateix SKU."""
    return {
        "entity_id": entity_id,
        "increment_id": "ES000088001",
        "store_id": store_id,
        "items": [
            {"item_id": 9001, "sku": sku, "qty_ordered": 2, "parent_item_id": None, "product_type": "configurable"},
            {"item_id": 9002, "sku": sku, "qty_ordered": 2, "parent_item_id": 9001, "product_type": "simple"},
        ],
        "extension_attributes": {"shipping_assignments": [{"shipping": {"method": method}}]},
    }


@tagged("post_install", "-at_install")
class TestMagentoLink(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backend = setup_backend(cls.env)
        cls.backend.store_view_ids.unlink()
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.env.company.id)], limit=1)
        cls.product = cls.env["product.product"].create({
            "name": "Guitarra de prova", "default_code": "013635", "type": "consu", "is_storable": True, "list_price": 100,
        })
        cls.partner = cls.env["res.partner"].create({"name": "Client Vinculació"})

    @contextmanager
    def _magento(self, orders=None, status=200, views=STORE_VIEWS):
        Client = type(self.env["backstage.client"])
        calls = []

        def get_order(_self, increment_id):
            calls.append(increment_id)
            if status != 200:
                return MagentoResponse(status, {"message": "down"} if status else None, error="timeout")
            return MagentoResponse(200, {"items": orders or [], "total_count": len(orders or [])})

        with patch.object(Client, "get_order_by_increment", get_order), \
                patch.object(Client, "get_store_views", lambda _self: MagentoResponse(200, views)):
            yield calls

    def _order(self, increment="ES000088001", store_code=False):
        order = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "warehouse_id": self.warehouse.id,
            "magento_increment_id": increment,
            "magento_store_code": store_code,
            "backstage_link_state": "pending",
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 2})],
        })
        return order

    # ------------------------------------------------------------------ vinculació

    def test_link_fills_store_method_and_parent_item(self):
        order = self._order()
        with self._magento([magento_order()]):
            self.assertEqual(order.backstage_link(), "linked")
        self.assertEqual(order.backstage_link_state, "linked")
        self.assertEqual(order.magento_order_id, 7001)
        self.assertEqual(order.magento_store_code, "es_es")
        self.assertEqual(order.magento_shipping_method, "instore_pickup")
        self.assertTrue(order.magento_is_pickup)
        # El pare del configurable és l'article que Magento factura i expedeix.
        self.assertEqual(order.order_line.magento_order_item_id, 9001)

    def test_store_views_are_cached(self):
        order = self._order()
        with self._magento([magento_order()]):
            order.backstage_link()
        self.assertEqual(
            sorted(self.backend.store_view_ids.mapped("code")), ["es_es", "pt_pt"])  # admin (0) no compta
        # Ja es coneixen: no es tornen a demanar a Magento.
        with patch.object(type(self.env["backstage.client"]), "get_store_views", side_effect=AssertionError("cached")):
            self.assertEqual(self.backend._store_views()[4], "pt_pt")

    def test_duplicate_increment_needs_store(self):
        orders = [magento_order(7001, store_id=1), magento_order(7002, store_id=4)]
        order = self._order()
        with self._magento(orders):
            self.assertEqual(order.backstage_link(), "error")
        self.assertEqual(order.backstage_link_state, "error")
        self.assertIn("2", order.backstage_link_error)
        self.assertTrue(order.activity_ids)

        # Amb la vista de botiga coneguda es tria la comanda correcta.
        order.write({"magento_store_code": "pt_pt", "backstage_link_state": "pending"})
        with self._magento(orders):
            self.assertEqual(order.backstage_link(), "linked")
        self.assertEqual(order.magento_order_id, 7002)

    def test_unmatched_line_is_error(self):
        order = self._order()
        with self._magento([magento_order(sku="ALTRE")]):
            self.assertEqual(order.backstage_link(), "error")
        self.assertIn("013635", order.backstage_link_error)
        self.assertFalse(order.order_line.magento_order_item_id)

    def test_not_found_is_error(self):
        order = self._order()
        with self._magento([]):
            self.assertEqual(order.backstage_link(), "error")

    def test_magento_down_is_retryable(self):
        order = self._order()
        with self._magento(status=503):
            with self.assertRaises(RetryableJobError):
                order.backstage_link()
            # Fora d'una tasca de cua no es propaga l'error: la comanda queda pendent.
            self.assertFalse(order._backstage_ensure_linked())
        self.assertEqual(order.backstage_link_state, "pending")

    def test_action_button_raises_on_error(self):
        order = self._order()
        with self._magento([]):
            try:
                order.action_backstage_link()
                raised = False
            except Exception:  # noqa: BLE001  (UserError; assertRaises faria rollback de l'estat)
                raised = True
        self.assertTrue(raised)

    # ------------------------------------------------------------------ emissió

    def test_enqueue_while_magento_down_fills_store_on_send(self):
        order = self._order()
        Outbox = self.env["backstage.outbox"]
        with self._magento(status=503), trap_jobs():
            outbox = Outbox.enqueue(order, "ready_for_pickup", "so-link-ready", {"pickup_location_code": "store_01"})
        self.assertTrue(outbox)
        self.assertFalse(json.loads(outbox.envelope)["store_code"])

        sent = []
        Client = type(self.env["backstage.client"])
        with self._magento([magento_order()]), patch.object(
            Client, "post_event", lambda _self, envelope: sent.append(envelope) or MagentoResponse(200, {"status": "completed"}),
        ):
            outbox.send_to_magento()
        self.assertEqual(outbox.state, "sent")
        self.assertEqual(sent[0]["store_code"], "es_es")
        self.assertEqual(json.loads(outbox.envelope)["store_code"], "es_es")

    def test_send_rejected_when_link_fails(self):
        order = self._order()
        Outbox = self.env["backstage.outbox"]
        with self._magento(status=503), trap_jobs():
            outbox = Outbox.enqueue(order, "ready_for_pickup", "so-link-fail", {})
        with self._magento([]):
            self.assertEqual(outbox.send_to_magento(), "rejected: order_not_linked")
        self.assertEqual(outbox.error_code, "order_not_linked")

    def test_enqueue_skipped_when_link_error(self):
        order = self._order()
        with self._magento([]):
            outbox = self.env["backstage.outbox"].enqueue(order, "ready_for_pickup", "so-link-err", {})
        # Queda a "Per revisar" (sense job) i la comanda té l'activitat de vinculació.
        self.assertEqual((outbox.state, outbox.error_code), ("rejected", "order_not_linked"))
        self.assertIn("Web order needs review", order.activity_ids.mapped("summary"))
        self.assertIn(outbox, self.env["backstage.outbox"].search(REVIEW_DOMAIN))

    def test_shipment_deferred_until_linked(self):
        order = self._order()
        order.action_confirm()
        picking = order.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing")
        with self._magento(status=503), trap_jobs() as trap:
            picking._backstage_emit_shipment()
            trap.assert_jobs_count(1, only=picking._backstage_emit_shipment)
        self.assertFalse(self.env["backstage.outbox"].search([("sale_order_id", "=", order.id)]))

    def test_shipment_of_unlinked_order_waits_in_review_and_resumes(self):
        order = self._order()
        order.action_confirm()
        picking = order.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing")
        self.env["stock.quant"]._update_available_quantity(self.product, self.warehouse.lot_stock_id, 2)
        picking.action_assign()
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        Outbox = self.env["backstage.outbox"]
        with self._magento([]), trap_jobs():          # Magento no troba la comanda
            picking._action_done()
        held = Outbox.search([("sale_order_id", "=", order.id)])
        self.assertEqual((held.event_type, held.state, held.error_code), ("pending_link", "rejected", "order_not_linked"))
        self.assertEqual(held.document_ref, picking)
        self.assertIn(held, Outbox.search(REVIEW_DOMAIN))
        self.assertIn(picking.name, held.problem)
        self.assertIn(picking.name, held.display_name)

        # Es corregeix i es torna a vincular: l'expedició s'envia sola i el recordatori desapareix.
        with self._magento([magento_order()]), trap_jobs():
            order.action_backstage_link()
        events = Outbox.search([("sale_order_id", "=", order.id)])
        self.assertFalse(held.exists())
        self.assertEqual(events.mapped("event_type"), ["collected"])
        self.assertEqual(events.state, "pending")

    # ------------------------------------------------------------------ tasca programada

    def test_cron_enqueues_pending(self):
        order = self._order(increment="ES000088077")
        order.action_confirm()
        linked = self._order(increment="ES000088078", store_code="es_es")
        linked.write({"backstage_link_state": "linked"})
        linked.action_confirm()
        with trap_jobs() as trap:
            self.env["sale.order"].cron_backstage_link_pending()
            descriptions = [job.description for job in trap.enqueued_jobs]
        self.assertTrue(any(order.name in d for d in descriptions))
        self.assertFalse(any(linked.name in d for d in descriptions))
