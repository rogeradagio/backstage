"""equivalència entre les fonts MSI de Magento i les ubicacions d'Odoo."""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class BackstageSource(models.Model):
    _name = "backstage.source"
    _description = "Magento source"
    _order = "source_code"
    _rec_name = "source_code"

    backend_id = fields.Many2one(
        "backstage.backend", required=True, ondelete="cascade", index=True,
        default=lambda self: self.env["backstage.backend"]._get(),
    )
    source_code = fields.Char(string="Code", required=True, help="MSI source_code (case sensitive).")
    name = fields.Char(help="Name of the source in Magento.")
    location_id = fields.Many2one(
        "stock.location", string="Odoo location", ondelete="restrict", index=True,
        domain=[("usage", "=", "internal")],
        help="Stock of this location (and its children) is the stock of the source.",
    )
    ship_from = fields.Boolean(
        string="Ships to customers", default=True,
        help="Untick for sources that never ship to a customer (e.g. supplier stock).",
    )
    export_stock = fields.Boolean(
        string="Export stock",
        help="Backstage sends the sellable stock of this source. Each source must be updated by "
             "a single system: leave it unticked while another system still updates it.",
    )
    active = fields.Boolean(default=True)

    _location_uniq = models.Constraint(
        "unique(location_id)", lambda env, diagnostics: env._("Each location can only belong to one source."),
    )
    _code_uniq = models.Constraint(
        "unique(backend_id, source_code)", lambda env, diagnostics: env._("Each Magento source can only be listed once."),
    )

    @api.constrains("export_stock", "location_id")
    def _check_export_location(self):
        for source in self:
            if source.export_stock and not source.location_id:
                raise ValidationError(_("Source %s needs an Odoo location before exporting its stock.", source.source_code))

    def _compute_display_name(self):
        for source in self:
            source.display_name = source.name and "%s (%s)" % (source.name, source.source_code) or source.source_code

    @api.model
    def source_for_location(self, location):
        """Font MSI d'una ubicació o de la seva ubicació pare més propera."""
        current = location
        Source = self.sudo()
        while current:
            mapping = Source.search([("location_id", "=", current.id)], limit=1)
            if mapping:
                return mapping
            current = current.location_id
        return Source.browse()

    @api.model
    def _propose_location(self, code, name):
        """Estoc del magatzem el codi o nom del qual coincideix amb la font, si encara no és d'una altra font."""
        Warehouse = self.env["stock.warehouse"]
        warehouse = Warehouse.search([("code", "=ilike", code)], limit=1)
        if not warehouse and name:
            warehouse = Warehouse.search([("name", "=ilike", name)], limit=1)
        location = warehouse.lot_stock_id
        if location and self.with_context(active_test=False).search_count([("location_id", "=", location.id)]):
            return self.env["stock.location"]
        return location
