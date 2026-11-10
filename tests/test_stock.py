"""comprova el càlcul i l’exportació de l’estoc vendible cap a Magento."""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.queue_job.tests.common import trap_jobs

from ..models.backstage_client import MagentoResponse
from .common import setup_backend



@tagged("post_install", "-at_install")
class TestMagentoStockExport(TransactionCase):
    """§6.4: cada comanda web es descompta una sola vegada (a Magento, via reserva MSI)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backend = setup_backend(cls.env, stock_enabled=True)

        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.env.company.id)], limit=1)
        cls.stock = cls.warehouse.lot_stock_id
        Source = cls.env["backstage.source"]
        cls.source = Source.with_context(active_test=False).search([("location_id", "=", cls.stock.id)])
        if cls.source:
            cls.source.write({"source_code": "store_01", "active": True, "export_stock": True})
        else:
            cls.source = Source.create({"location_id": cls.stock.id, "source_code": "store_01", "export_stock": True})
        # Aïllament: només exporta la font d'aquesta prova (la BD pot tenir altres fonts configurades)
        Source.search([("id", "!=", cls.source.id)]).write({"export_stock": False})
        cls.product = cls.env["product.product"].create({
            "name": "Producte estoc", "default_code": "STK-1", "type": "consu", "is_storable": True,
        })
        cls.env["stock.quant"]._update_available_quantity(cls.product, cls.stock, 10)
        cls.partner = cls.env["res.partner"].create({"name": "Client estoc"})
        cls.Stock = cls.env["backstage.stock"]

    def _sellable(self):
        return self.Stock.compute_sellable(self.product, self.source)[(self.product.id, "store_01")]

    def _order(self, qty, magento=True):
        values = {
            "partner_id": self.partner.id,
            "warehouse_id": self.warehouse.id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": qty, "magento_order_item_id": 1})],
        }
        if magento:
            values.update(magento_increment_id="ES-STK-%s" % qty, magento_store_code="es_es", magento_shipping_method="tablerate_bestway")
        order = self.env["sale.order"].create(values)
        order.action_confirm()
        order.picking_ids.action_assign()
        return order

    def test_web_order_reservation_is_not_subtracted(self):
        self.assertEqual(self._sellable(), 10)
        self._order(2, magento=True)
        # Odoo reserva 2 (free_qty 8), però la reserva ja la fa MSI: la font continua a 10
        self.assertEqual(self._sellable(), 10)

    def test_other_channel_reservation_is_subtracted(self):
        self._order(3, magento=False)
        self.assertEqual(self._sellable(), 7)

    def test_shipped_but_not_confirmed_in_magento_keeps_units(self):
        web = self._order(2, magento=True)
        self._order(3, magento=False)
        self.assertEqual(self._sellable(), 7)
        picking = web.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing")
        with trap_jobs():
            for move in picking.move_ids:
                move.quantity = move.product_uom_qty
                move.picked = True
            picking._action_done()
        outbox = web.backstage_outbox_ids.filtered(lambda o: o.event_type == "shipment_dispatched")
        self.assertEqual(outbox.state, "pending")
        # Odoo ja ha expedit (a mà 8) però Magento encara té la reserva: la font no ha de baixar encara
        self.assertEqual(self._sellable(), 7)
        outbox.state = "sent"
        # Enviament confirmat a Magento: MSI ha descomptat la font; el valor real ja és 8 - 3.
        self.assertEqual(self._sellable(), 5)

    def test_transfer_serving_web_order_is_not_subtracted(self):
        web = self._order(1, magento=True)
        transfer = self.env["stock.picking"].create({
            "picking_type_id": self.warehouse.int_type_id.id,
            "location_id": self.stock.id,
            "location_dest_id": self.stock.id,
            "sale_id": web.id,
            "move_ids": [(0, 0, {
                "product_id": self.product.id, "product_uom_qty": 2, "product_uom": self.product.uom_id.id,
                "location_id": self.stock.id, "location_dest_id": self.stock.id,
            })],
        })
        transfer.action_confirm()
        transfer.action_assign()
        self.assertEqual(self._sellable(), 10)

    def test_reservation_marks_dirty_and_export_sends_absolute_quantities(self):
        self.product.backstage_stock_dirty = False
        self._order(3, magento=False)
        self.assertTrue(self.product.backstage_stock_dirty)

        sent = []
        with patch.object(type(self.env["backstage.client"]), "post_source_items",
                          lambda _self, items: sent.extend(items) or MagentoResponse(200, [])):
            self.Stock.export_products(self.product.ids)
        self.assertEqual(sent, [{"sku": "STK-1", "source_code": "store_01", "quantity": 7.0, "status": 1}])
        self.assertFalse(self.product.backstage_stock_dirty)

    def test_cron_only_when_enabled(self):
        self.backend.stock_enabled = False
        self.product.backstage_stock_dirty = True
        with trap_jobs() as trap:
            self.Stock.cron_export_dirty()
            trap.assert_jobs_count(0)
        self.backend.stock_enabled = True
        with trap_jobs() as trap:
            self.Stock.cron_export_dirty()
            self.assertGreaterEqual(len(trap.enqueued_jobs), 1)
