"""comandes web: dades de Magento, vinculació amb la comanda de Magento i disparadors d'esdeveniments."""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.queue_job.exception import RetryableJobError

from .backstage_outbox import CHANNEL, REVIEW_DOMAIN

_logger = logging.getLogger(__name__)

PICKUP_METHOD = "instore_pickup"


class SaleOrder(models.Model):
    """Del connector existent només es fa servir el número de comanda (external_sales_order_ref).
    La resta (ID, store view, mètode d'enviament, línies) es demana a Magento, de manera que el mòdul
    funciona igual amb el connector, sense ell o amb un import propi en el futur."""

    _inherit = "sale.order"

    # ------------------------------------------------------------------ dades de Magento
    magento_increment_id = fields.Char(string="Magento order", copy=False, index=True)
    magento_order_id = fields.Integer(string="Magento order ID", copy=False)
    magento_store_code = fields.Char(string="Store view", copy=False)
    magento_shipping_method = fields.Char(string="Shipping method", copy=False)
    magento_is_pickup = fields.Boolean(string="Store pickup", compute="_compute_magento_is_pickup", store=True)

    # ------------------------------------------------------------------ estat de Backstage
    backstage_link_state = fields.Selection(
        [("pending", "Pending"), ("linked", "Linked"), ("error", "Not linked")],
        string="Magento link", copy=False, index=True,
    )
    backstage_link_error = fields.Text(string="Link problem", copy=False)
    backstage_outbox_ids = fields.One2many("backstage.outbox", "sale_order_id", string="Magento events")
    backstage_review_count = fields.Integer(compute="_compute_backstage_review_count")
    backstage_can_notify_ready = fields.Boolean(compute="_compute_backstage_can_notify_ready")

    @api.depends("magento_shipping_method")
    def _compute_magento_is_pickup(self):
        for order in self:
            order.magento_is_pickup = order.magento_shipping_method == PICKUP_METHOD

    def _compute_backstage_review_count(self):
        counts = dict(self.env["backstage.outbox"].sudo()._read_group(
            REVIEW_DOMAIN + [("sale_order_id", "in", self.ids)], ["sale_order_id"], ["__count"],
        ))
        for order in self:
            order.backstage_review_count = counts.get(order._origin, 0)

    @api.depends("picking_ids.state", "picking_ids.move_ids.state", "magento_is_pickup")
    def _compute_backstage_can_notify_ready(self):
        for order in self:
            picking = order.magento_is_pickup and order.magento_increment_id and order._backstage_pickup_picking()
            # L'albarà surt "Llest" encara que la reserva sigui parcial: cal que cada línia estigui reservada del tot.
            order.backstage_can_notify_ready = bool(
                picking and picking.state == "assigned"
                and all(move.state == "assigned" for move in picking.move_ids if move.state != "cancel")
                and not order._backstage_pending_supply()
            )

    def _backstage_notify(self, summary, note):
        """Activitat de Backstage per al venedor de la comanda."""
        self.ensure_one()
        return self.activity_schedule(
            "backstage.mail_activity_type_backstage",
            summary=summary,
            note=note,
            user_id=self.user_id.id or self.env.uid,
        )

    # ------------------------------------------------------------------ recollida a botiga

    def _backstage_pickup_picking(self):
        """Albarà de lliurament al client pendent de la recollida."""
        self.ensure_one()
        return self.picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing"
            and p.location_dest_id.usage == "customer"
            and p.state not in ("done", "cancel")
        )[:1]

    def _backstage_pending_supply(self):
        """Traspassos encara oberts que alimenten la comanda (origin = nom de la comanda)."""
        self.ensure_one()
        return self.env["stock.picking"].search([
            ("origin", "=", self.name),
            ("picking_type_code", "in", ("internal", "outgoing", "incoming")),
            ("location_dest_id.usage", "!=", "customer"),
            ("state", "not in", ("done", "cancel")),
        ])

    def action_backstage_ready_for_pickup(self):
        """La botiga confirma que té la mercaderia: s'avisa el client."""
        for order in self:
            if not order.backstage_can_notify_ready:
                raise UserError(_(
                    "%s is not ready yet: the pickup transfer must be fully reserved "
                    "and no transfer to the store may be pending.", order.name,
                ))
            picking = order._backstage_pickup_picking()
            source = self.env["backstage.source"].source_for_location(picking.location_id)
            self.env["backstage.outbox"].enqueue(
                order, "ready_for_pickup", "so-%s-ready" % order.id,
                {"pickup_location_code": source.source_code or ""},
                notification="send",
            )
            order.message_post(body=_("Customer notified that the order is ready for pickup."))
        return True

    def action_view_backstage_review(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("backstage.action_backstage_review")
        action["domain"] = REVIEW_DOMAIN + [("sale_order_id", "=", self.id)]
        return action

    # ------------------------------------------------------------------ disparadors

    def write(self, vals):
        result = super().write(vals)
        if vals.get("substate_id"):
            substate = self.env["backstage.backend"]._get().preparing_substate_id
            if substate and substate.id == vals["substate_id"]:
                for order in self.filtered("magento_increment_id"):
                    order._backstage_emit_preparing()
        return result

    def _backstage_preparing_sources(self):
        """Fonts d'on surt la mercaderia: albarans oberts de la comanda; si no n'hi ha, el magatzem."""
        self.ensure_one()
        Source = self.env["backstage.source"]
        pickings = self.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        codes = []
        for location in pickings.location_id or self.warehouse_id.lot_stock_id:
            code = Source.source_for_location(location).source_code
            if code and code not in codes:
                codes.append(code)
        return codes

    def _backstage_emit_preparing(self):
        """Subestat de preparació (comanda enviada a un magatzem extern) → preparing_at_warehouse."""
        self.ensure_one()
        codes = self._backstage_preparing_sources()
        if not codes:
            self._backstage_notify(
                _("Magento: unknown source"),
                _("The customer was not told the order is being prepared: no location of the order "
                  "belongs to a Magento source (Backstage → Configuration → Sources)."),
            )
            return
        self.env["backstage.outbox"].enqueue(
            self, "preparing_at_warehouse", "so-%s-preparing" % self.id, {"origin_source_codes": codes},
            notification="send",
        )

    def _action_cancel(self):
        result = super()._action_cancel()
        outbox = self.env["backstage.outbox"]
        for order in self.filtered("magento_increment_id"):
            outbox.enqueue(order, "cancelled", "so-%s-cancel" % order.id, {"reason": "Cancelled in Odoo"}, notification="send")
        return result

    # ------------------------------------------------------------------ vinculació

    def _backstage_legacy_reference(self):
        """Número de comanda Magento segons el connector actual (si és instal·lat)."""
        self.ensure_one()
        if "external_sales_order_ref" in self._fields and self.external_sales_order_ref:
            integration = self.integration_id if "integration_id" in self._fields else False
            if not integration or integration.type_api == "magento2":
                return self.external_sales_order_ref
        return False

    def action_confirm(self):
        result = super().action_confirm()
        for order in self.filtered(lambda o: not o.magento_increment_id):
            reference = order._backstage_legacy_reference()
            if reference:
                order.write({"magento_increment_id": reference, "backstage_link_state": "pending"})
                order.with_delay(channel=CHANNEL, description=_("Magento: link %s", order.name)).backstage_link()
        return result

    def action_backstage_link(self):
        for order in self:
            if not (order.magento_increment_id or order._backstage_legacy_reference()):
                raise UserError(_("%s has no Magento order number.", order.name))
            if not order.magento_increment_id:
                order.magento_increment_id = order._backstage_legacy_reference()
            order.backstage_link()
            if order.backstage_link_state == "error":
                raise UserError(order.backstage_link_error)
        return True

    def backstage_link(self):
        """Job: completa store view, mètode d'enviament i línies a partir de Magento."""
        self.ensure_one()
        response = self.env["backstage.client"].get_order_by_increment(self.magento_increment_id)
        if not response.ok:
            if response.retryable:
                raise RetryableJobError(_("Magento %s: %s", response.status, response.error or response.body), seconds=300)
            return self._backstage_link_failed(_("Magento answered %(status)s: %(message)s",
                                                 status=response.status, message=response.message))
        items = (response.body or {}).get("items") or []
        store_views = self.env["backstage.backend"]._get()._store_views()
        if self.magento_store_code:
            store_ids = {sid for sid, code in store_views.items() if code == self.magento_store_code}
            items = [i for i in items if i.get("store_id") in store_ids] or items
        if not items:
            return self._backstage_link_failed(_(
                "We could not find web order %s. Check that the order number is correct.", self.magento_increment_id))
        if len(items) > 1:
            return self._backstage_link_failed(_(
                "%(count)s Magento orders share the number %(ref)s. Fill in the store view of the order.",
                count=len(items), ref=self.magento_increment_id,
            ))
        magento = items[0]
        line_ids, errors = self._backstage_match_lines(magento.get("items") or [])
        if errors:
            return self._backstage_link_failed(_(
                "These products are not in the Magento order: %s", ", ".join(errors)))
        for line, item_id in line_ids.items():
            line.magento_order_item_id = item_id
        self.write({
            "magento_order_id": magento.get("entity_id"),
            "magento_store_code": store_views.get(magento.get("store_id")) or self.magento_store_code,
            "magento_shipping_method": self._backstage_shipping_method(magento),
            "backstage_link_state": "linked",
            "backstage_link_error": False,
        })
        self.env["backstage.outbox"]._resume_after_link(self)
        return "linked"

    def _backstage_ensure_linked(self, raise_retry=False):
        """Abans de crear un esdeveniment: si la comanda encara no està vinculada, s'intenta ara (síncron).

        Si Magento no respon, dins d'un job es propaga el RetryableJobError (el job es reintenta);
        fora d'un job retorna False i la comanda queda 'pending'.
        """
        self.ensure_one()
        if not self.env["backstage.outbox"]._is_enabled():
            return True
        if self.magento_store_code and self.backstage_link_state in (False, "linked"):
            return True
        if self.backstage_link_state != "linked":
            try:
                self.backstage_link()
            except RetryableJobError as exc:
                if raise_retry:
                    raise
                _logger.warning("Magento link for %s not possible now: %s", self.name, exc)
        return bool(self.magento_store_code) and self.backstage_link_state == "linked"

    def _backstage_link_or_defer(self, record, method):
        """Per als emissors que necessiten els order_item_id (lliuraments, factures).

        True → la comanda està vinculada i es pot construir el payload.
        False → s'ha ajornat (job que es reintenta fins que Magento respongui) o la vinculació ha fallat:
        el document queda retingut a "Per revisar" i es torna a generar quan la comanda es vinculi.
        """
        self.ensure_one()
        in_job = bool(self.env.context.get("job_uuid"))
        if self._backstage_ensure_linked(raise_retry=in_job):
            return True
        if self.backstage_link_state == "error":
            self.env["backstage.outbox"]._hold_until_linked(self, record, method)
        elif not in_job:
            getattr(record.with_delay(
                channel=CHANNEL, max_retries=50,
                description=_("Magento: %(doc)s (waiting for %(order)s to be linked)", doc=record.display_name, order=self.name),
            ), method)()
        return False

    def _backstage_link_failed(self, message):
        self.write({"backstage_link_state": "error", "backstage_link_error": message})
        self._backstage_notify(_("Web order needs review"), message)
        return "error"

    @api.model
    def _backstage_shipping_method(self, magento):
        for assignment in (magento.get("extension_attributes") or {}).get("shipping_assignments") or []:
            method = ((assignment or {}).get("shipping") or {}).get("method")
            if method:
                return method
        return magento.get("shipping_method") or False

    def _backstage_match_lines(self, magento_items):
        """Casa cada línia de producte d'Odoo amb un item de Magento.

        1. Si el connector té l'ID de la línia i coincideix amb un item, es fa servir.
        2. Si no, per SKU, preferint els items sense pare (configurables: el pare porta l'SKU del fill).
        """
        by_id = {item.get("item_id"): item for item in magento_items}
        free = list(magento_items)
        result, errors = {}, []
        lines = self.order_line.filtered(
            lambda l: l.product_id and not l.display_type and not l.is_delivery
            and l.product_id.type != "service"
        )
        for line in lines:
            legacy = line.integration_external_id if "integration_external_id" in line._fields else False
            item = None
            if legacy and str(legacy).isdigit() and int(legacy) in by_id:
                item = by_id[int(legacy)]
            if item is None:
                sku = line.product_id.default_code
                candidates = [i for i in free if i.get("sku") == sku]
                candidates.sort(key=lambda i: (i.get("parent_item_id") is not None, abs((i.get("qty_ordered") or 0) - line.product_uom_qty)))
                item = candidates[0] if candidates else None
            if item is None:
                errors.append(line.product_id.default_code or line.product_id.display_name)
                continue
            if item in free:
                free.remove(item)
            result[line] = item["item_id"]
        return result, errors

    @api.model
    def cron_backstage_link_pending(self, limit=200):
        """Comandes obertes amb número de Magento però sense vincular (o amb error recuperable)."""
        domain = [("state", "=", "sale"), ("backstage_link_state", "in", (False, "pending"))]
        if "external_sales_order_ref" in self._fields:
            domain = ["|", ("magento_increment_id", "!=", False), ("external_sales_order_ref", "!=", False)] + domain
        else:
            domain = [("magento_increment_id", "!=", False)] + domain
        for order in self.search(domain, limit=limit, order="id desc"):
            if not order.magento_increment_id:
                reference = order._backstage_legacy_reference()
                if not reference:
                    continue
                order.magento_increment_id = reference
            order.with_delay(channel=CHANNEL, description=_("Magento: link %s", order.name)).backstage_link()
