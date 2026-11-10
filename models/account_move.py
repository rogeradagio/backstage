"""factures i abonaments publicats → esdeveniments invoice_issued / credit_note_issued."""

import hashlib
import json

from odoo import _, models


class AccountMove(models.Model):
    _inherit = "account.move"

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        for move in posted.filtered(lambda m: m.move_type in ("out_invoice", "out_refund")):
            move._backstage_emit_document()
        return posted

    def _backstage_order(self):
        orders = self.invoice_line_ids.sale_line_ids.order_id.filtered("magento_increment_id")
        return orders[:1] if len(orders) == 1 else self.env["sale.order"]

    def _backstage_document_payload(self):
        """Línies, ports i total. Retorna (payload, errors)."""
        self.ensure_one()
        items, errors, shipping = [], [], 0.0
        for line in self.invoice_line_ids.filtered(lambda l: l.display_type == "product"):
            sale_line = line.sale_line_ids[:1]
            # Línia de ports (stock_delivery): va a shipping_amount, no és cap línia de Magento.
            if sale_line.is_delivery:
                shipping += line.price_total
                continue
            if not sale_line or not sale_line.magento_order_item_id:
                errors.append(line.product_id.display_name or line.name)
                continue
            items.append({
                "order_item_id": sale_line.magento_order_item_id,
                "sku": line.product_id.default_code or "",
                "qty": line.quantity,
                "row_total_incl_tax": round(line.price_total, 2),
            })
        payload = {
            "issued_at": (self.invoice_date or self.date).strftime("%Y-%m-%dT00:00:00Z"),
            "currency": self.currency_id.name,
            "items": items,
            "shipping_amount": round(shipping, 2),
            "grand_total": round(self.amount_total, 2),
        }
        return payload, errors

    def _backstage_emit_document(self):
        """Factura publicada → invoice_issued. Rectificativa publicada → credit_note_issued. Sempre suppress."""
        self.ensure_one()
        order = self._backstage_order()
        if not order:
            return
        if not order._backstage_link_or_defer(self, "_backstage_emit_document"):
            return
        payload, errors = self._backstage_document_payload()
        if errors:
            order._backstage_notify(
                _("Magento: %s not sent", self.name),
                _("%(doc)s was not sent to Magento: these lines have no Magento line (%(products)s). "
                  "Link the order again from its Magento tab.", doc=self.name, products=", ".join(errors)),
            )
            return
        outbox = self.env["backstage.outbox"]
        if self.move_type == "out_invoice":
            payload.update(odoo_invoice_id=self.id, odoo_invoice_number=self.name)
            event_id = self._backstage_event_id(payload)
            payload["payment_state"] = self.payment_state
            outbox.enqueue(order, "invoice_issued", event_id, payload, record=self)
        elif self.reversed_entry_id:
            payload.update(
                odoo_credit_note_id=self.id,
                odoo_credit_note_number=self.name,
                odoo_invoice_id=self.reversed_entry_id.id,
                refund_mode="offline",
            )
            outbox.enqueue(order, "credit_note_issued", self._backstage_event_id(payload), payload, record=self)

    def _backstage_event_id(self, payload):
        """move-<id>-<empremta>: republicar el document igual no genera res de nou;
        republicar-lo corregit (tornat a esborrany i modificat) genera un esdeveniment nou."""
        fingerprint = hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:10]
        return "move-%s-%s" % (self.id, fingerprint)
