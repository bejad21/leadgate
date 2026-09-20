{
    "name": "LeadGate Domain",
    "summary": "Generic catalog model shared across LeadGate domain adapters (cars, real estate).",
    "description": """
Provides the domain-agnostic leadgate.catalog.item model used by the
LeadGate engine and its domain adapters (cars / real estate) to query
catalog inventory regardless of vertical.
""",
    "version": "18.0.1.0.0",
    "category": "Sales/CRM",
    "author": "LeadGate",
    "license": "LGPL-3",
    "depends": ["base", "crm", "base_automation"],
    "data": [
        "security/ir.model.access.csv",
        "views/catalog_item_views.xml",
        "data/automated_action.xml",
        "data/cron.xml",
    ],
    "installable": True,
    "application": False,
}
