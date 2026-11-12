"""assistent on la botiga indica el motiu de cada diferència en validar una recepció web."""

from odoo import fields, models

from ..models.backstage_web_receipt_incident import REASONS
from ..models.stock_picking import INCIDENTS_DECLARED_KEY


class BackstageWebReceiptIncidentWizard(models.TransientModel):
    _name = "backstage.web.receipt.incident.wizard"
    _description = "Reasons for web receipt differences"

    picking_id = fields.Many2one("stock.picking", required=True, readonly=True)
    line_ids = fields.One2many("backstage.web.receipt.incident.wizard.line", "wizard_id")

    def action_confirm(self):
        """Desa els motius (esborrany) i continua la validació estàndard (backorder inclòs)."""
        self.ensure_one()
        picking = self.picking_id
        Incident = self.env["backstage.web.receipt.incident"]
        # La botiga no pot esborrar incidències (així no pot fer desaparèixer les obertes):
        # només s'esborren, amb sudo, els esborranys d'aquesta recepció (es tornen a crear tot seguit).
        Incident.search([("picking_id", "=", picking.id), ("state", "=", "draft")]).sudo().unlink()
        Incident.create([{
            "picking_id": picking.id,
            "product_id": line.product_id.id,
            "qty_expected": line.qty_expected,
            "qty_received": line.qty_received,
            "reason": line.reason,
            "note": line.note,
        } for line in self.line_ids])
        return picking.with_context(**{INCIDENTS_DECLARED_KEY: True}).button_validate()


class BackstageWebReceiptIncidentWizardLine(models.TransientModel):
    _name = "backstage.web.receipt.incident.wizard.line"
    _description = "Web receipt difference"

    wizard_id = fields.Many2one("backstage.web.receipt.incident.wizard", required=True, ondelete="cascade")
    product_id = fields.Many2one("product.product", required=True, readonly=True)
    qty_expected = fields.Float(string="Sent", readonly=True, digits="Product Unit")
    qty_received = fields.Float(string="Received", readonly=True, digits="Product Unit")
    reason = fields.Selection([r for r in REASONS if r[0] != "unspecified"], required=True)
    note = fields.Char(string="Comment")
