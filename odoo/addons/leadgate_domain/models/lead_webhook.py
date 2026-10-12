import json
import logging
import urllib.error
import urllib.request

from odoo import models

_logger = logging.getLogger(__name__)

WEBHOOK_URL_PARAM = "leadgate.sync_webhook_url"
WEBHOOK_TIMEOUT_SECONDS = 5


class CatalogItem(models.Model):
    _inherit = "leadgate.catalog.item"

    def _notify_webhook(self):
        """POST the id, domain_type, status, name and price of each record to the URL in the
        `leadgate.sync_webhook_url` system parameter.

        Called by the "Catalog item status changed" Automated Action (see data/automated_action.xml). If the
        parameter is unset or empty this is a silent no-op: it must never raise or block a record write.
        """
        webhook_url = self.env["ir.config_parameter"].sudo().get_param(WEBHOOK_URL_PARAM)
        if not webhook_url:
            return

        for record in self:
            payload = json.dumps(
                {
                    "id": record.id,
                    "domain_type": record.domain_type,
                    "status": record.status,
                    "name": record.name,
                    "price": record.price,
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS)
            except (urllib.error.URLError, OSError) as exc:
                _logger.warning(
                    "leadgate.catalog.item(%s): failed to notify webhook %s: %s",
                    record.id,
                    webhook_url,
                    exc,
                )
