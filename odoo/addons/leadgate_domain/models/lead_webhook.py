import json
import logging
import urllib.error
import urllib.request

from odoo import fields, models

_logger = logging.getLogger(__name__)

WEBHOOK_URL_PARAM = "leadgate.status_webhook_url"
WEBHOOK_SECRET_PARAM = "leadgate.status_webhook_secret"
WEBHOOK_SECRET_HEADER = "X-Odoo-Webhook-Secret"
WEBHOOK_TIMEOUT_SECONDS = 5


class CatalogItem(models.Model):
    _inherit = "leadgate.catalog.item"

    def _notify_webhook(self):
        """POST the id, domain_type, status, name, price and write time of each record to the engine's
        /webhook/odoo-status, at the URL in the `leadgate.status_webhook_url` system parameter, with the
        shared secret from `leadgate.status_webhook_secret` in the X-Odoo-Webhook-Secret header.

        Called by the "Catalog item status changed" Automated Action (see data/automated_action.xml). If
        either parameter is unset or empty this is a silent no-op: it must never raise or block a record
        write, and it never sends without the secret.
        """
        params = self.env["ir.config_parameter"].sudo()
        webhook_url = params.get_param(WEBHOOK_URL_PARAM)
        secret = params.get_param(WEBHOOK_SECRET_PARAM)
        if not webhook_url or not secret:
            return
        if not webhook_url.lower().startswith(("http://", "https://")):
            # urllib would also open file: and ftp: addresses; the secret must only ever go to a web address.
            _logger.warning("leadgate.status_webhook_url must start with http:// or https://; not sending")
            return

        for record in self:
            payload = json.dumps(
                {
                    "id": record.id,
                    "domain_type": record.domain_type,
                    "status": record.status,
                    "name": record.name,
                    "price": record.price,
                    "write_date": fields.Datetime.to_string(record.write_date),
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                webhook_url,
                data=payload,
                headers={"Content-Type": "application/json", WEBHOOK_SECRET_HEADER: secret},
                method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS)
            except (urllib.error.URLError, OSError) as exc:
                # The secret is in the request headers, never in the URL or the message logged here.
                _logger.warning(
                    "leadgate.catalog.item(%s): failed to notify the status webhook: %s",
                    record.id,
                    exc,
                )
