"""comprova que els permisos no es poden saltar per RPC: cua d'esdeveniments, incidències i configuració."""

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.queue_job.tests.common import trap_jobs

from ..models.backstage_outbox import NOT_LINKED
from .common import setup_backend


@tagged("post_install", "-at_install")
class TestBackstageSecurity(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backend = setup_backend(cls.env)
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.env.company.id)], limit=1)
        cls.product = cls.env["product.product"].create({
            "name": "Producte de seguretat", "default_code": "SEC-1", "type": "consu", "is_storable": True,
        })
        cls.partner = cls.env["res.partner"].create({"name": "Client Seguretat"})
        cls.backstage_user = new_test_user(
            cls.env, login="bk_sec_user", groups="base.group_user,backstage.group_backstage_user")
        cls.stock_user = new_test_user(cls.env, login="bk_sec_stock", groups="stock.group_stock_user")
        cls.stock_manager = new_test_user(cls.env, login="bk_sec_stock_mgr", groups="stock.group_stock_manager")

    def _order(self):
        order = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "warehouse_id": self.warehouse.id,
            "magento_increment_id": "ES000099001",
            "magento_store_code": "es_es",
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1, "magento_order_item_id": 1})],
        })
        order.action_confirm()
        return order

    def _rejected_outbox(self, order):
        with trap_jobs():
            outbox = self.env["backstage.outbox"].enqueue(order, "cancelled", "sec-cancel", {}, notification="send")
        outbox.write({"state": "rejected", "error_code": "transition_not_allowed"})
        return outbox

    # ------------------------------------------------------------------ cua d'esdeveniments

    def test_user_cannot_write_outbox(self):
        outbox = self._rejected_outbox(self._order())
        with self.assertRaises(AccessError):
            outbox.with_user(self.backstage_user).write({"envelope": "{}"})
        with self.assertRaises(AccessError):
            outbox.with_user(self.backstage_user).write({"retry_method": "unlink", "res_model": "res.partner"})

    def test_user_can_still_retry_and_discard(self):
        outbox = self._rejected_outbox(self._order())
        with trap_jobs() as trap:
            outbox.with_user(self.backstage_user).action_retry()
            trap.assert_jobs_count(1, only=outbox.send_to_magento)
        self.assertEqual(outbox.state, "pending")
        outbox.state = "rejected"
        outbox.with_user(self.backstage_user).action_discard()
        self.assertEqual(outbox.state, "discarded")

    def test_non_backstage_user_cannot_retry(self):
        outbox = self._rejected_outbox(self._order())
        with self.assertRaises(AccessError):
            outbox.with_user(self.stock_user).action_retry()

    def test_retry_method_must_be_whitelisted(self):
        order = self._order()
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env["backstage.outbox"].create({
                "event_id": "sec-hold-bad", "event_type": "pending_link", "sale_order_id": order.id,
                "res_model": "res.partner", "res_id": self.partner.id, "envelope": "{}",
                "notification_mode": "suppress", "state": "rejected", "error_code": NOT_LINKED,
                "retry_method": "unlink",
            })

    def test_resume_ignores_tampered_method(self):
        """Encara que algú escrigui directament a la base de dades, no s'executa cap mètode fora de la llista."""
        order = self._order()
        outbox = self.env["backstage.outbox"].create({
            "event_id": "sec-hold", "event_type": "pending_link", "sale_order_id": order.id,
            "res_model": "account.move", "res_id": 0, "envelope": "{}",
            "notification_mode": "suppress", "state": "rejected", "error_code": NOT_LINKED,
            "retry_method": "_backstage_emit_document",
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE backstage_outbox SET res_model = 'res.partner', res_id = %s, retry_method = 'unlink' WHERE id = %s",
            (self.partner.id, outbox.id),
        )
        outbox.invalidate_recordset()
        self.env["backstage.outbox"]._resume_after_link(order)
        self.assertTrue(self.partner.exists())
        self.assertTrue(outbox.exists())

    # ------------------------------------------------------------------ incidències

    def _incident(self):
        picking = self.env["stock.picking"].create({
            "picking_type_id": self.warehouse.in_type_id.id,
            "location_id": self.env.ref("stock.stock_location_suppliers").id,
            "location_dest_id": self.warehouse.lot_stock_id.id,
        })
        return self.env["backstage.web.receipt.incident"].create({
            "picking_id": picking.id, "product_id": self.product.id,
            "qty_expected": 2, "qty_received": 1, "reason": "short", "state": "open",
        })

    def test_store_cannot_resolve_incident(self):
        incident = self._incident()
        as_store = incident.with_user(self.stock_user)
        as_store.note = "Faltava una caixa"  # declarar sí que pot
        with self.assertRaises(AccessError):
            as_store.write({"resolution": "Reenviat"})
        with self.assertRaises(AccessError):
            as_store.write({"state": "resolved"})
        with self.assertRaises(AccessError):
            as_store.action_resolve()
        self.assertEqual(incident.state, "open")

    def test_manager_resolves_and_reopens_incident(self):
        incident = self._incident().with_user(self.stock_manager)
        self.assertTrue(incident.can_resolve)
        self.assertFalse(incident.with_user(self.stock_user).can_resolve)
        incident.resolution = "Reenviat"
        incident.action_resolve()
        self.assertEqual(incident.state, "resolved")
        with self.assertRaises(AccessError):
            incident.with_user(self.stock_user).action_reopen()
        incident.action_reopen()
        self.assertEqual(incident.state, "open")

    # ------------------------------------------------------------------ configuració

    def test_user_cannot_run_configuration_actions(self):
        backend = self.backend.with_user(self.backstage_user)
        for action in ("action_test_connection", "action_sync_sources", "action_import_from_connector",
                       "action_export_stock_now", "action_enable", "action_disable"):
            with self.subTest(action=action), self.assertRaises(AccessError):
                getattr(backend, action)()
