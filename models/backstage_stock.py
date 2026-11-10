"""calcula l'estoc vendible i l'exporta a les fonts MSI de Magento."""

import logging
from collections import defaultdict

from odoo import _, api, models

from odoo.addons.queue_job.exception import RetryableJobError

from .backstage_outbox import CHANNEL, SHIPMENT_EVENTS

_logger = logging.getLogger(__name__)


class BackstageStock(models.AbstractModel):
    """Export d'estoc a les fonts MSI (§6.4 de l'especificació).

    font(p, L) = estoc a mà a L
               − reservat a L per a demanda que NO és de Magento
               + expedit a Odoo des de L però amb el shipment encara no confirmat a Magento

    Cada comanda web es descompta una sola vegada: a Magento (reserva MSI) fins que el shipment existeix.
    """

    _name = "backstage.stock"
    _description = "Stock export to Magento"

    # ------------------------------------------------------------------ càlcul

    @api.model
    def _is_magento_move(self, move, web_order_names=frozenset()):
        """El moviment serveix una comanda web: la seva o un traspàs que l'alimenta (també lligat només per origin)."""
        orders = move.sale_line_id.order_id | move.picking_id.sale_id | move.reference_ids.sale_ids
        if any(orders.mapped("magento_increment_id")):
            return True
        return bool({move.origin, move.picking_id.origin} & web_order_names)

    @api.model
    def _web_order_names(self, lines):
        origins = {o for o in lines.move_id.mapped("origin") + lines.move_id.picking_id.mapped("origin") if o}
        if not origins:
            return frozenset()
        orders = self.env["sale.order"].sudo().search([("name", "in", list(origins)), ("magento_increment_id", "!=", False)])
        return frozenset(orders.mapped("name"))

    @api.model
    def compute_breakdown(self, products, sources):
        """{(product_id, source_code): {on_hand, reserved_other, shipped_unsent, sellable}}."""
        result = {}
        if not products or not sources:
            return result
        Quant = self.env["stock.quant"].sudo()
        MoveLine = self.env["stock.move.line"].sudo()
        unsent = self._unsent_shipment_pickings()
        for source in sources.filtered("location_id"):
            location = source.location_id
            on_hand = defaultdict(float)
            for quant in Quant.search([("product_id", "in", products.ids), ("location_id", "child_of", location.id)]):
                on_hand[quant.product_id.id] += quant.quantity

            # Mercaderia en trànsit cap a aquesta botiga per a comandes web: no és a cap font, però MSI la té
            # reservada; es compta a la font de destí perquè la reserva no la descompti dues vegades.
            transits = self.env["stock.warehouse"].sudo().search([
                ("lot_stock_id", "=", location.id), ("backstage_transit_location_id", "!=", False),
            ]).backstage_transit_location_id
            if transits:
                for quant in Quant.search([("product_id", "in", products.ids), ("location_id", "child_of", transits.ids)]):
                    on_hand[quant.product_id.id] += quant.quantity

            reserved_other = defaultdict(float)
            pending_lines = MoveLine.search([
                ("product_id", "in", products.ids),
                ("location_id", "child_of", location.id),
                ("state", "not in", ("done", "cancel")),
            ])
            web_names = self._web_order_names(pending_lines)
            for line in pending_lines:
                if not self._is_magento_move(line.move_id, web_names):
                    reserved_other[line.product_id.id] += line.quantity_product_uom

            shipped_unsent = defaultdict(float)
            if unsent:
                for line in MoveLine.search([
                    ("product_id", "in", products.ids),
                    ("location_id", "child_of", location.id),
                    ("state", "=", "done"),
                    ("picking_id", "in", unsent.ids),
                ]):
                    shipped_unsent[line.product_id.id] += line.quantity_product_uom

            for product in products:
                qty = on_hand[product.id] - reserved_other[product.id] + shipped_unsent[product.id]
                result[(product.id, source.source_code)] = {
                    "on_hand": on_hand[product.id],
                    "reserved_other": reserved_other[product.id],
                    "shipped_unsent": shipped_unsent[product.id],
                    "sellable": max(0.0, qty),
                }
        return result

    @api.model
    def compute_sellable(self, products, sources):
        """Retorna {(product_id, source_code): quantitat} per a cada producte i font."""
        return {key: values["sellable"] for key, values in self.compute_breakdown(products, sources).items()}

    @api.model
    def _unsent_shipment_pickings(self):
        """Albarans validats a Odoo amb el shipment encara no creat a Magento (outbox ni enviat ni descartat)."""
        outbox = self.env["backstage.outbox"].sudo().search([
            ("event_type", "in", SHIPMENT_EVENTS),
            ("state", "not in", ("sent", "discarded")),
            ("res_model", "=", "stock.picking"),
        ])
        return self.env["stock.picking"].sudo().browse(outbox.mapped("res_id")).exists()

    # ------------------------------------------------------------------ exportació

    @api.model
    def _is_enabled(self):
        backend = self.env["backstage.backend"]._get()
        return bool(backend.stock_enabled and backend._is_sending())

    @api.model
    def _export_sources(self):
        return self.env["backstage.source"].sudo().search([("export_stock", "=", True), ("location_id", "!=", False)])

    @api.model
    def _exportable_products(self, domain=None):
        return self.env["product.product"].sudo().search([
            ("is_storable", "=", True),
            ("default_code", "!=", False),
            ("product_tmpl_id.backstage_stock_sync", "=", True),
        ] + (domain or []))

    @api.model
    def enqueue_export(self, products):
        """Un job per lot de productes."""
        size = self.env["backstage.backend"]._get().stock_batch_size or 200
        for start in range(0, len(products), size):
            batch = products[start:start + size]
            # Mateix canal (seqüencial) que els esdeveniments: l'exportació no s'avança a un enviament pendent.
            self.with_delay(channel=CHANNEL, description=_("Magento: stock of %s products", len(batch))).export_products(batch.ids)

    @api.model
    def export_products(self, product_ids):
        """Job: calcula i envia les quantitats absolutes a Magento."""
        products = self.env["product.product"].sudo().browse(product_ids).exists()
        sources = self._export_sources()
        if not products or not sources:
            return "nothing to export"
        sellable = self.compute_sellable(products, sources)
        by_id = {p.id: p for p in products}
        items = [
            {"sku": by_id[product_id].default_code, "source_code": source_code, "quantity": qty, "status": 1 if qty > 0 else 0}
            for (product_id, source_code), qty in sellable.items()
        ]
        response = self.env["backstage.client"].post_source_items(items)
        if not response.ok:
            if response.retryable:
                raise RetryableJobError(_("Magento stock export %s: %s", response.status, response.error or response.body), seconds=120)
            _logger.error("Magento stock export rejected (%s): %s", response.status, response.body)
            return "rejected: %s" % response.status
        products.write({"backstage_stock_dirty": False})
        return "exported %s items" % len(items)

    # ------------------------------------------------------------------ tasques programades

    @api.model
    def cron_export_dirty(self):
        """Exporta els productes amb canvis d'estoc o de reserves (cada pocs minuts)."""
        if not self._is_enabled():
            return
        products = self._exportable_products([("backstage_stock_dirty", "=", True)])
        if products:
            self.enqueue_export(products)

    @api.model
    def cron_export_full(self):
        """Reconciliació completa (de nit): corregeix qualsevol desviació acumulada."""
        if not self._is_enabled():
            return
        self.enqueue_export(self._exportable_products())
