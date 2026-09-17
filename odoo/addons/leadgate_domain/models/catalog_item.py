# odoo/addons/leadgate_domain/models/catalog_item.py
from odoo import fields, models


class CatalogItem(models.Model):
    _name = "leadgate.catalog.item"
    _description = "Domain-agnostic catalog item (car or property)"

    name = fields.Char(required=True)
    domain_type = fields.Selection(
        [("cars", "Cars"), ("real_estate", "Real Estate")], required=True, index=True
    )
    attributes = fields.Text(help="JSON-encoded domain-specific fields")
    price = fields.Float(required=True)
    status = fields.Selection(
        [("available", "Available"), ("reserved", "Reserved"), ("sold", "Sold")],
        default="available",
        required=True,
    )
