"""centralitza les crides HTTP a les API de Magento i el tractament de les respostes."""

import json
import logging
import re

import requests

from odoo import _, api, models

_logger = logging.getLogger(__name__)

EVENTS_ENDPOINT = "/rest/V1/backstage/events"
SOURCE_ITEMS_ENDPOINT = "/rest/V1/inventory/source-items"
SOURCES_ENDPOINT = "/rest/V1/inventory/sources"
ORDERS_ENDPOINT = "/rest/V1/orders"
STORE_VIEWS_ENDPOINT = "/rest/V1/store/storeViews"


class MagentoResponse:
    def __init__(self, status, body, error=None):
        self.status = status
        self.body = body
        self.error = error

    @property
    def ok(self):
        return self.status is not None and 200 <= self.status < 300

    @property
    def retryable(self):
        """Errors de xarxa i 5xx: es poden reintentar amb el mateix event_id."""
        return self.status is None or self.status >= 500 or self.status == 409 and self.code == "event_in_progress"

    @property
    def raw_message(self):
        return (self.body or {}).get("message", "") if isinstance(self.body, dict) else ""

    @property
    def code(self):
        """Codi de negoci del bridge: el missatge comença per [codi]."""
        message = self.raw_message
        if message.startswith("[") and "]" in message:
            return message[1:message.index("]")]
        return None

    @property
    def message(self):
        """Missatge llegible: sense el [codi] i amb els paràmetres de Magento (%1, %nom) substituïts."""
        message = self.raw_message
        if self.code:
            message = message[message.index("]") + 1:].strip()
        parameters = (self.body or {}).get("parameters") if isinstance(self.body, dict) else None
        if isinstance(parameters, list):
            message = re.sub(r"%(\d+)", lambda m: str(parameters[int(m.group(1)) - 1])
                             if 0 < int(m.group(1)) <= len(parameters) else m.group(0), message)
        elif isinstance(parameters, dict):
            message = re.sub(r"%(\w+)", lambda m: str(parameters.get(m.group(1), m.group(0))), message)
        return message or self.error or ""


class BackstageClient(models.AbstractModel):
    """Client HTTP cap a Magento, aïllat per poder-lo substituir a les proves."""

    _name = "backstage.client"
    _description = "Backstage HTTP client for Magento"

    @api.model
    def post_event(self, envelope):
        return self._request("POST", EVENTS_ENDPOINT, body={"event": envelope})

    @api.model
    def post_source_items(self, items):
        """Quantitats absolutes per font MSI (MSI: POST /V1/inventory/source-items)."""
        return self._request("POST", SOURCE_ITEMS_ENDPOINT, body={"sourceItems": items})

    @api.model
    def get_order_by_increment(self, increment_id):
        """Comanda de Magento per increment_id (pot n'hi haver més d'una si es repeteix entre botigues)."""
        return self._request("GET", ORDERS_ENDPOINT, params={
            "searchCriteria[filter_groups][0][filters][0][field]": "increment_id",
            "searchCriteria[filter_groups][0][filters][0][value]": increment_id,
            "searchCriteria[pageSize]": 5,
        })

    @api.model
    def get_store_views(self):
        return self._request("GET", STORE_VIEWS_ENDPOINT)

    @api.model
    def get_sources(self):
        return self._request("GET", SOURCES_ENDPOINT, params={"searchCriteria[pageSize]": 500})

    @api.model
    def _request(self, method, endpoint, params=None, body=None):
        backend = self.env["backstage.backend"]._get()
        base_url = (backend.url or "").rstrip("/")
        token = backend.sudo().token or ""
        if not base_url or not token:
            return MagentoResponse(None, None, _("Magento URL or token not configured"))
        headers = {"Authorization": "Bearer " + token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = requests.request(
                method, base_url + endpoint, params=params, headers=headers,
                data=json.dumps(body) if body is not None else None, timeout=backend.timeout or 30,
            )
        except requests.RequestException as exc:
            return MagentoResponse(None, None, str(exc))
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text[:500]}
        return MagentoResponse(response.status_code, payload)
