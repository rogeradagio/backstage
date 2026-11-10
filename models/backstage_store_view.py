"""store views de Magento, a quina botiga pertany cada comanda."""

from odoo import fields, models


class BackstageStoreView(models.Model):
    _name = "backstage.store.view"
    _description = "Magento store view"
    _order = "magento_id"

    backend_id = fields.Many2one("backstage.backend", required=True, ondelete="cascade", index=True)
    magento_id = fields.Integer(string="Magento ID", required=True)
    code = fields.Char(required=True)
    name = fields.Char()

    _magento_id_uniq = models.Constraint(
        "unique(backend_id, magento_id)", lambda env, diagnostics: env._("Each store view can only be listed once."),
    )
