"""desa i envia de manera fiable els esdeveniments de fulfilment cap a Magento."""

import json
import logging
from datetime import datetime, timezone

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.job import identity_exact

from .backstage_client import MagentoResponse

_logger = logging.getLogger(__name__)

CHANNEL = "root.backstage"
SUPPRESS_CONTEXT_KEY = "backstage_suppress"
SHIPMENT_EVENTS = ("collected", "shipment_dispatched")

# Errors temporals: a partir d'aquests intents, algú hauria de mirar si Magento funciona.
STALE_ATTEMPTS = 5
REVIEW_DOMAIN = ["|", ("state", "=", "rejected"), "&", ("state", "=", "failed"), ("attempts", ">=", STALE_ATTEMPTS)]

EVENT_TYPES = [
    ("ready_for_pickup", "Ready for pickup"),
    ("collected", "Collected"),
    ("shipment_dispatched", "Shipped"),
    ("delivered", "Delivered"),
    ("preparing_at_warehouse", "Preparing at warehouse"),
    ("in_transit_to_store", "In transit to store"),
    ("invoice_issued", "Invoice"),
    ("credit_note_issued", "Credit note"),
    ("return_received", "Return received"),
    ("cancelled", "Cancelled"),
    # No és cap esdeveniment de Magento: un document que no s'ha pogut enviar perquè la comanda no està vinculada.
    ("pending_link", "Not sent: order not linked"),
]
NOT_LINKED = "order_not_linked"

# Documents que es poden retenir fins que la comanda es vinculi, i l'únic mètode que els torna a generar.
# _resume_after_link l'executa amb sudo: mai s'ha de cridar un mètode que no sigui en aquesta llista.
HOLD_METHODS = {
    "stock.picking": ("_backstage_emit_shipment", "_backstage_emit_return"),
    "account.move": ("_backstage_emit_document",),
}


class BackstageOutbox(models.Model):
    """Cua transaccional: cada fet d'Odoo crea un registre a la mateixa transacció,
    i un job de queue_job l'envia després a Magento. El payload es congela en crear-lo,
    de manera que un reintent envia exactament el mateix (mateix hash a Magento)."""

    _name = "backstage.outbox"
    _description = "Magento event"
    _order = "id desc"

    event_id = fields.Char(string="Event ID", required=True, readonly=True, index=True)
    event_type = fields.Selection(EVENT_TYPES, string="Event", required=True, readonly=True, index=True)
    sale_order_id = fields.Many2one("sale.order", string="Order", readonly=True, index=True, ondelete="set null")
    res_model = fields.Char(readonly=True)
    res_id = fields.Many2oneReference(model_field="res_model", readonly=True)
    document_ref = fields.Reference(
        [("sale.order", "Order"), ("stock.picking", "Transfer"), ("account.move", "Invoice")],
        string="Document", compute="_compute_document_ref",
    )
    envelope = fields.Text(required=True, readonly=True, help="Full envelope sent to Magento (JSON).")
    notification_mode = fields.Selection(
        [("send", "Customer notified"), ("suppress", "Silent")], string="Customer", required=True, readonly=True,
    )
    state = fields.Selection(
        [
            ("pending", "Queued"),
            ("sent", "Sent"),
            ("failed", "Retrying"),
            ("rejected", "Rejected"),
            ("discarded", "Discarded"),
        ],
        default="pending", required=True, index=True, readonly=True,
    )
    http_status = fields.Integer(string="HTTP status", readonly=True)
    error_code = fields.Char(readonly=True)
    response = fields.Text(readonly=True)
    attempts = fields.Integer(readonly=True)
    sent_at = fields.Datetime(readonly=True)
    magento_document_ids = fields.Char(
        string="Magento documents", readonly=True, help="Invoices, shipments or credit memos created in Magento.")
    problem = fields.Text(string="What happened", compute="_compute_explanation")
    next_step = fields.Text(string="What to do", compute="_compute_explanation")
    activity_id = fields.Many2one("mail.activity", readonly=True, ondelete="set null", copy=False)
    retry_method = fields.Char(
        readonly=True, help="pending_link: method of the document that builds the event once the order is linked.")
    envelope_display = fields.Text(compute="_compute_json_display")
    response_display = fields.Text(compute="_compute_json_display")

    _event_id_uniq = models.Constraint(
        "unique(event_id)", lambda env, diagnostics: env._("The event ID must be unique."),
    )

    @api.constrains("res_model", "retry_method")
    def _check_retry_method(self):
        for outbox in self.filtered("retry_method"):
            if outbox.retry_method not in HOLD_METHODS.get(outbox.res_model, ()):
                raise ValidationError(_(
                    "%(method)s cannot be used to rebuild a %(model)s document.",
                    method=outbox.retry_method, model=outbox.res_model,
                ))

    @api.depends("event_type", "sale_order_id.name", "res_model", "res_id")
    @api.depends_context("lang")
    def _compute_display_name(self):
        labels = dict(self._fields["event_type"]._description_selection(self.env))
        for outbox in self:
            label = labels.get(outbox.event_type, outbox.event_type)
            if outbox.event_type == "pending_link" and outbox.document_ref:
                label = _("%s not sent", outbox.document_ref.display_name)
            outbox.display_name = "%s · %s" % (label, outbox.sale_order_id.name) if outbox.sale_order_id else label

    @api.depends("res_model", "res_id")
    def _compute_document_ref(self):
        allowed = dict(self._fields["document_ref"].selection)
        for outbox in self:
            record = outbox.res_model in allowed and self.env[outbox.res_model].browse(outbox.res_id).exists()
            outbox.document_ref = "%s,%s" % (record._name, record.id) if record else False

    @api.depends("envelope", "response")
    def _compute_json_display(self):
        def pretty(text):
            try:
                data = json.loads(text or "")
            except ValueError:
                return text
            if isinstance(data, dict) and isinstance(data.get("payload"), str):
                try:
                    data["payload"] = json.loads(data["payload"])
                except ValueError:
                    pass
            return json.dumps(data, indent=2, ensure_ascii=False)

        for outbox in self:
            outbox.envelope_display = pretty(outbox.envelope)
            outbox.response_display = pretty(outbox.response)

    @api.depends("state", "error_code", "http_status", "response", "attempts")
    @api.depends_context("lang")
    def _compute_explanation(self):
        for outbox in self:
            outbox.problem, outbox.next_step = outbox._explain()

    def _explain(self):
        """(què ha passat, què cal fer) en paraules de l'usuari."""
        self.ensure_one()
        if self.state == "failed":
            return (
                _("Magento is not answering (%(status)s). Tried %(count)s times.",
                  status=self.http_status or _("no connection"), count=self.attempts),
                _("Nothing to do: it keeps retrying. If it lasts, check that the store is up.")
                if self.attempts >= STALE_ATTEMPTS else _("Nothing to do: it will be retried automatically."),
            )
        if self.state != "rejected":
            return False, False
        if self.error_code == NOT_LINKED:
            what = self.document_ref.display_name if self.retry_method and self.document_ref else _("This event")
            return (
                _("%(what)s was not sent: the order could not be matched with a Magento order (%(error)s)",
                  what=what, error=self.response or ""),
                _("Fix the order number in the Magento tab of the order and press Link again: "
                  "everything waiting for the link is then sent automatically."),
            )
        if self.http_status in (401, 403):
            return (
                _("Magento rejected the access token or its permissions."),
                _("Check the connection in Backstage → Configuration, then retry."),
            )
        if self.error_code == "has_shipments":
            return (
                _("The order is already shipped in Magento, so it can no longer be cancelled."),
                _("Handle it as a return (validate the return and issue the credit note), then discard this cancellation."),
            )
        if self.error_code == "invoice_requires_credit_note":
            return (
                _("The order has an invoice in Magento, so it cannot be cancelled yet."),
                _("Issue the credit note for the invoice first, then retry the cancellation."),
            )
        if self.error_code == "transition_not_allowed":
            return (
                _("Magento does not accept this step for the current status of the order."),
                _("Check the order in Magento. If the step is no longer needed, discard it."),
            )
        message = self._response_message()
        return (
            _("Magento rejected the event: %s", message) if message
            else _("Magento rejected the event (HTTP %s).", self.http_status),
            _("Fix the cause in Odoo or Magento and retry. Discard it if it is no longer needed."),
        )

    def _response_message(self):
        try:
            body = json.loads(self.response or "")
        except ValueError:
            return self.response or ""
        if not isinstance(body, dict):
            return ""
        return MagentoResponse(self.http_status, body).message

    # ------------------------------------------------------------------ creació

    @api.model
    def _is_enabled(self):
        return self.env["backstage.backend"]._get()._is_sending()

    @api.model
    def enqueue(self, order, event_type, event_id, payload, notification="suppress", template=None, record=None):
        """Crea l'esdeveniment i el job d'enviament. Idempotent per event_id."""
        backend = self.env["backstage.backend"]._get()
        if not backend._is_sending() or not order.magento_increment_id:
            return self.browse()
        existing = self.sudo().search([("event_id", "=", event_id)], limit=1)
        if existing:
            return existing
        if self.env.context.get(SUPPRESS_CONTEXT_KEY) or backend._is_silent():
            notification = "suppress"
        # Si Magento no respon, l'esdeveniment es desa i el codi de botiga es completa en enviar-lo.
        linked = order._backstage_ensure_linked()
        envelope = {
            "event_id": event_id,
            "event_type": event_type,
            "store_code": order.magento_store_code,
            "order_reference": order.magento_increment_id,
            "occurred_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "notification": {"mode": notification, "template": template, "comment": None},
            "payload": json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        }
        values = {
            "event_id": event_id,
            "event_type": event_type,
            "sale_order_id": order.id,
            "res_model": record._name if record else "sale.order",
            "res_id": record.id if record else order.id,
            "envelope": json.dumps(envelope, ensure_ascii=False),
            "notification_mode": notification,
        }
        if not linked and order.backstage_link_state == "error":
            # Queda a "Per revisar"; la comanda ja té l'activitat de vinculació.
            values.update(state="rejected", error_code="order_not_linked", response=order.backstage_link_error)
            return self.sudo().create(values)
        outbox = self.sudo().create(values)
        event_label = dict(self._fields["event_type"]._description_selection(self.env))[event_type]
        outbox._enqueue_job(_("Magento: %(type)s %(order)s", type=event_label, order=order.name))
        return outbox

    @api.model
    def _hold_until_linked(self, order, record, method):
        """El document no es pot convertir en esdeveniment (cal la comanda de Magento): queda a "Per revisar"."""
        event_id = "hold-%s-%s-%s" % (record._name.replace(".", "_"), record.id, method.strip("_"))
        existing = self.sudo().search([("event_id", "=", event_id)], limit=1)
        if existing:
            existing.write({"state": "rejected", "response": order.backstage_link_error})
            return existing
        return self.sudo().create({
            "event_id": event_id,
            "event_type": "pending_link",
            "sale_order_id": order.id,
            "res_model": record._name,
            "res_id": record.id,
            "envelope": "{}",
            "notification_mode": "suppress",
            "state": "rejected",
            "error_code": NOT_LINKED,
            "response": order.backstage_link_error,
            "retry_method": method,
        })

    @api.model
    def _resume_after_link(self, order):
        """La comanda ja està vinculada: es tornen a generar els documents retinguts i a enviar els rebutjats."""
        held = self.sudo().search([
            ("sale_order_id", "=", order.id), ("state", "=", "rejected"), ("error_code", "=", NOT_LINKED),
        ], order="id")  # en l'ordre en què van passar: l'expedició abans que la factura
        documents, events = held.filtered("retry_method"), held.filtered(lambda o: not o.retry_method)
        for outbox in documents:
            method = outbox.retry_method
            if method not in HOLD_METHODS.get(outbox.res_model, ()):
                # La restricció ho impedeix; això només pot venir d'una escriptura directa a la base de dades.
                _logger.warning("Backstage: held event %s ignored: %s.%s is not allowed",
                                outbox.event_id, outbox.res_model, method)
                continue
            record = self.env[outbox.res_model].sudo().browse(outbox.res_id).exists()
            outbox.unlink()
            if record:
                getattr(record, method)()
        for outbox in events:
            outbox.write({"state": "pending"})
            outbox._enqueue_job(_("Magento (after link): %s", outbox.event_id))

    def _enqueue_job(self, description):
        self.with_delay(
            channel=CHANNEL, identity_key=identity_exact, max_retries=20, description=description,
        ).send_to_magento()

    # ------------------------------------------------------------------ enviament

    def send_to_magento(self):
        self.ensure_one()
        if self.state in ("sent", "discarded"):
            return "already %s" % self.state
        envelope = json.loads(self.envelope)
        if not envelope.get("store_code"):
            order = self.sale_order_id
            if not order._backstage_ensure_linked(raise_retry=True):
                self.write({"state": "rejected", "error_code": "order_not_linked", "response": order.backstage_link_error})
                return "rejected: order_not_linked"
            envelope["store_code"] = order.magento_store_code
            self.envelope = json.dumps(envelope, ensure_ascii=False)
        response = self.env["backstage.client"].post_event(envelope)
        values = {
            "attempts": self.attempts + 1,
            "http_status": response.status or 0,
            "response": json.dumps(response.body, ensure_ascii=False) if response.body is not None else response.error,
            "error_code": response.code if not response.ok else False,
        }
        if response.ok:
            body = response.body or {}
            documents = {
                key: body.get(key) for key in ("magento_invoice_ids", "magento_shipment_ids", "magento_creditmemo_ids")
                if body.get(key)
            }
            values.update(state="sent", sent_at=fields.Datetime.now(), magento_document_ids=json.dumps(documents) if documents else False)
            self.write(values)
            self._close_activity(_("Sent to Magento."))
            self._release_shipped_stock()
            return "sent: %s" % body.get("status")

        if response.retryable:
            values["state"] = "failed"
            self.write(values)
            # El reintent reverteix la transacció de la tasca de cua: l'estat també es desa amb un cursor propi
            # perquè quedi visible a la cua de sortida (si el registre encara no és confirmat, no hi ha res a desar).
            try:
                with self.env.registry.cursor() as cr:
                    self.with_env(self.env(cr=cr)).write(values)
            except Exception:  # noqa: BLE001
                _logger.warning("Could not persist failed state for %s", self.event_id)
            raise RetryableJobError(
                _("Magento %s: %s", response.status, response.error or response.code),
                seconds=min(3600, 60 * (2 ** min(self.attempts, 6))),
            )

        values["state"] = "rejected"
        self.write(values)
        self._notify_rejection()
        return "rejected: %s" % (response.code or response.status)

    def _release_shipped_stock(self):
        """L'enviament ja existeix a Magento (o no hi arribarà): la unitat deixa de sumar-se a la font."""
        for outbox in self.filtered(lambda o: o.res_model == "stock.picking" and o.event_type in SHIPMENT_EVENTS):
            self.env["stock.picking"].browse(outbox.res_id).exists().move_ids.product_id._backstage_mark_dirty()

    def _notify_rejection(self):
        """Error de negoci (4xx): no es reintenta. Activitat a la comanda perquè el venedor ho sàpiga."""
        for outbox in self.filtered("sale_order_id"):
            problem, next_step = outbox._explain()
            outbox.activity_id = outbox.sale_order_id._backstage_notify(
                _("Magento rejected: %s", outbox.display_name),
                "%s<br/>%s" % (problem, next_step),
            )

    def _close_activity(self, feedback):
        for outbox in self.filtered("activity_id"):
            outbox.activity_id.action_feedback(feedback=feedback)

    # ------------------------------------------------------------------ accions

    def _check_backstage_user(self):
        """Els usuaris no poden escriure a la cua (només llegir-la): les accions escriuen amb sudo
        després de comprovar el grup, i només canvien l'estat, mai el contingut de l'esdeveniment."""
        if not self.env.su and not self.env.user.has_group("backstage.group_backstage_user"):
            raise AccessError(_("Only Backstage users can retry or discard Magento events."))

    def action_retry(self):
        """Reintent manual (mateix event_id i payload). Un document retingut torna a provar la vinculació."""
        self._check_backstage_user()
        for outbox in self:
            if outbox.state not in ("rejected", "discarded"):
                raise UserError(_("%s is not waiting for a retry.", outbox.display_name))
            if outbox.retry_method:
                # Si la vinculació funciona, _resume_after_link el torna a generar i l'esborra.
                # Amb els permisos de l'usuari sobre la comanda.
                outbox.sale_order_id.action_backstage_link()
                continue
            outbox.sudo().write({"state": "pending"})
            outbox.sudo()._enqueue_job(_("Magento (retry): %s", outbox.event_id))
        return True

    def action_discard(self):
        """Ja no cal enviar-lo (p. ex. s'ha resolt a mà a Magento)."""
        self._check_backstage_user()
        for outbox in self:
            if outbox.state not in ("rejected", "failed"):
                raise UserError(_("Only rejected or retrying events can be discarded."))
        self.sudo().write({"state": "discarded"})
        self._close_activity(_("Discarded by %s.", self.env.user.name))
        self._release_shipped_stock()
        return True
