"""connexió amb Magento: un únic registre que conté tota la configuració de Backstage."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .backstage_outbox import REVIEW_DOMAIN


class BackstageBackend(models.Model):
    _name = "backstage.backend"
    _description = "Magento connection"
    _inherit = ["mail.thread"]

    name = fields.Char(required=True, default="Magento", tracking=True)
    # Dos interruptors independents: si s'envia res a Magento, i si Magento avisa els clients.
    enabled = fields.Boolean(
        tracking=True, help="When off, nothing is sent to Magento.")
    silent = fields.Boolean(
        string="Stealth mode", default=True, tracking=True,
        help="Everything is sent to Magento, but Magento sends no e-mail to customers about their orders.")

    # ------------------------------------------------------------------ connexió
    url = fields.Char(string="Store URL", tracking=True, help="e.g. https://shop.example.com")
    token = fields.Char(
        string="Access token", copy=False, groups="backstage.group_backstage_manager",
        help="Access token of the Magento integration (see the module README for the permissions it needs).",
    )
    timeout = fields.Integer(string="Timeout (s)", default=30)
    connection_state = fields.Selection(
        [("unknown", "Not checked"), ("ok", "Connected"), ("error", "Error")],
        default="unknown", required=True, readonly=True, copy=False,
    )
    connection_message = fields.Text(readonly=True, copy=False)
    connection_checked_at = fields.Datetime(string="Last check", readonly=True, copy=False)
    store_view_ids = fields.One2many("backstage.store.view", "backend_id", string="Store views")

    # ------------------------------------------------------------------ fonts i botigues
    source_ids = fields.One2many("backstage.source", "backend_id", string="Sources")
    unmapped_source_count = fields.Integer(compute="_compute_unmapped_source_count")
    warehouse_ids = fields.Many2many("stock.warehouse", compute="_compute_warehouse_ids", string="Warehouses")
    connector_available = fields.Boolean(compute="_compute_connector_available")

    # ------------------------------------------------------------------ avisos i estoc
    preparing_substate_id = fields.Many2one(
        "base.substate", string="Preparing sub-state",
        # Només subestats de comanda confirmada: la preparació passa després de confirmar.
        domain=[("model", "=", "sale.order"), ("target_state_value_id.target_state_value", "=", "sale")],
        help="When an order reaches this sub-state (e.g. sent to an external warehouse), "
             "the customer is told the order is being prepared.",
    )
    delivered_enabled = fields.Boolean(
        string="Notify carrier delivery",
        help="Tell Magento when carrier tracking marks a home delivery as delivered.",
    )
    stock_enabled = fields.Boolean(
        string="Export stock", tracking=True,
        help="Send the sellable quantity of every source marked \"Export stock\".",
    )
    stock_batch_size = fields.Integer(string="Products per batch", default=200)

    # ------------------------------------------------------------------ comptadors
    review_count = fields.Integer(compute="_compute_counts")
    incident_count = fields.Integer(compute="_compute_counts")
    unlinked_order_count = fields.Integer(compute="_compute_counts")

    @api.depends("source_ids.location_id", "source_ids.active")
    def _compute_unmapped_source_count(self):
        for backend in self:
            backend.unmapped_source_count = len(backend.source_ids.filtered(lambda s: not s.location_id))

    def _compute_warehouse_ids(self):
        warehouses = self.env["stock.warehouse"].search([])
        for backend in self:
            backend.warehouse_ids = warehouses

    def _compute_connector_available(self):
        available = "external.stock.location.line" in self.env
        for backend in self:
            backend.connector_available = available

    def _compute_counts(self):
        Outbox = self.env["backstage.outbox"].sudo()
        Incident = self.env["backstage.web.receipt.incident"].sudo()
        Order = self.env["sale.order"].sudo()
        review = Outbox.search_count(REVIEW_DOMAIN)
        incidents = Incident.search_count([("state", "=", "open")])
        unlinked = Order.search_count([("backstage_link_state", "=", "error"), ("state", "!=", "cancel")])
        for backend in self:
            backend.review_count = review
            backend.incident_count = incidents
            backend.unlinked_order_count = unlinked

    # ------------------------------------------------------------------ accés

    @api.model
    def _get(self):
        """La connexió de Backstage (n'hi ha una sola)."""
        backend = self.env.ref("backstage.backend_magento", raise_if_not_found=False)
        return (backend or self.search([], limit=1)).sudo()

    def _is_sending(self):
        return self.enabled

    def _is_silent(self):
        return self.silent

    def _check_manager(self):
        """Les accions de configuració escriuen amb sudo (fonts, store views) o encuen feina:
        el permís de lectura dels usuaris no ha de bastar per cridar-les per RPC."""
        if not self.env.su and not self.env.user.has_group("backstage.group_backstage_manager"):
            raise AccessError(_("Only Backstage administrators can change the Magento connection."))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_main(self):
        raise UserError(_("The Magento connection cannot be deleted. Switch it off instead."))

    # ------------------------------------------------------------------ connexió

    def action_test_connection(self):
        """Comprova URL, token i permisos, i porta les store views i les fonts de Magento."""
        self._check_manager()
        self.ensure_one()
        client = self.env["backstage.client"]
        views = client.get_store_views()
        if not views.ok or not isinstance(views.body, list):
            return self._connection_failed(views, _("store views"))
        self._sync_store_views(views.body)
        sources = client.get_sources()
        if not sources.ok:
            return self._connection_failed(sources, _("inventory sources"))
        self._sync_sources((sources.body or {}).get("items") or [])
        self.write({
            "connection_state": "ok",
            "connection_checked_at": fields.Datetime.now(),
            "connection_message": _(
                "Connected: %(views)s store views and %(sources)s sources.",
                views=len(self.store_view_ids), sources=len(self.source_ids),
            ),
        })
        return True

    def _connection_failed(self, response, what):
        if response.status is None:
            message = _("Cannot reach the store (%(error)s). Check the store URL.", error=response.error)
        elif response.status == 401:
            message = _("Magento rejected the access token. Check the token.")
        elif response.status == 403:
            message = _("The integration has no permission to read %(what)s. Grant it in Magento "
                        "(System → Integrations).", what=what)
        elif response.status == 404:
            message = _("This URL does not answer as a Magento REST API. Check the store URL.")
        else:
            message = _("Magento answered %(status)s: %(message)s", status=response.status, message=response.message)
        self.write({
            "connection_state": "error",
            "connection_checked_at": fields.Datetime.now(),
            "connection_message": message,
        })
        return True

    def _sync_store_views(self, views):
        StoreView = self.env["backstage.store.view"].sudo()
        existing = {view.magento_id: view for view in self.store_view_ids}
        for data in views:
            if not data.get("id"):  # 0 = admin
                continue
            values = {"code": data.get("code"), "name": data.get("name") or data.get("code")}
            if data["id"] in existing:
                existing.pop(data["id"]).write(values)
            else:
                StoreView.create(dict(values, backend_id=self.id, magento_id=data["id"]))
        for view in existing.values():
            view.unlink()

    def _sync_sources(self, items):
        """Crea les fonts que encara no hi són i n'actualitza el nom. Proposa una ubicació si el nom coincideix."""
        Source = self.env["backstage.source"].sudo().with_context(active_test=False)
        for item in items:
            code = item.get("source_code")
            if not code:
                continue
            source = Source.search([("backend_id", "=", self.id), ("source_code", "=", code)], limit=1)
            if source:
                source.name = item.get("name") or source.name
                continue
            Source.create({
                "backend_id": self.id,
                "source_code": code,
                "name": item.get("name"),
                "location_id": Source._propose_location(code, item.get("name")).id,
            })

    def _store_views(self):
        """{store_id: code}. Si encara no es coneixen, es demanen a Magento."""
        self.ensure_one()
        if not self.store_view_ids:
            response = self.env["backstage.client"].get_store_views()
            if response.ok and isinstance(response.body, list):
                self._sync_store_views(response.body)
        return {view.magento_id: view.code for view in self.store_view_ids}

    # ------------------------------------------------------------------ estat

    def action_enable(self):
        self._check_manager()
        self.ensure_one()
        if self.connection_state != "ok":
            raise UserError(_("Test the connection first."))
        self.enabled = True

    def action_disable(self):
        self._check_manager()
        self.enabled = False

    def action_notify_customers(self):
        self._check_manager()
        self.silent = False

    def action_silent(self):
        self._check_manager()
        self.silent = True

    # ------------------------------------------------------------------ fonts

    def action_sync_sources(self):
        self._check_manager()
        self.ensure_one()
        response = self.env["backstage.client"].get_sources()
        if not response.ok:
            self._connection_failed(response, _("inventory sources"))
            raise UserError(self.connection_message)
        self._sync_sources((response.body or {}).get("items") or [])
        return True

    def action_import_from_connector(self):
        """Crea les fonts a partir del mapatge d'un connector existent (external.stock.location.line)."""
        self._check_manager()
        self.ensure_one()
        if not self.connector_available:
            raise UserError(_("No connector with a source mapping is installed."))
        Source = self.env["backstage.source"].sudo().with_context(active_test=False)
        for line in self.env["external.stock.location.line"].sudo().search([]):
            location, code = line.erp_location_id, line.external_location_id.code
            if not location or not code:
                continue
            source = Source.search([("backend_id", "=", self.id), ("source_code", "=", code)], limit=1)
            if not source:
                Source.create({"backend_id": self.id, "source_code": code, "location_id": location.id})
            elif not source.location_id:
                source.location_id = location
        return True

    # ------------------------------------------------------------------ estoc

    def action_export_stock_now(self):
        self._check_manager()
        self.ensure_one()
        if not self.stock_enabled:
            raise UserError(_("Stock export is switched off."))
        self.env["backstage.stock"].cron_export_full()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "success", "message": _("Full stock export queued."), "sticky": False},
        }

    # ------------------------------------------------------------------ navegació

    def action_view_review(self):
        return self.env["ir.actions.act_window"]._for_xml_id("backstage.action_backstage_review")

    def action_view_incidents(self):
        return self.env["ir.actions.act_window"]._for_xml_id("backstage.action_backstage_web_receipt_incident")

    def action_view_unlinked_orders(self):
        action = self.env["ir.actions.act_window"]._for_xml_id("backstage.action_backstage_web_orders")
        action["context"] = {"search_default_link_error": 1}
        return action
