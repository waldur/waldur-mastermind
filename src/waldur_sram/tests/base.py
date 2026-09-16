"""Shared SRAM test scaffolding: an SBS-like client and an authenticated base."""

import json
import urllib.parse

from constance.test.unittest import override_config
from rest_framework import test
from rest_framework.authtoken.models import Token

from waldur_core.structure.tests import factories as structure_factories
from waldur_sram.tests import payloads

BASE = "/scim/v2/sram"


class SbsClient:
    """Mirrors how SBS (server/scim/scim.py, sweep.py) talks to a service."""

    def __init__(self, client):
        self.client = client

    def _send(self, method, url, body=None):
        kwargs = {"HTTP_ACCEPT": "application/scim+json"}
        if body is not None:
            kwargs["data"] = json.dumps(body)
            kwargs["content_type"] = "application/scim+json"
        return getattr(self.client, method)(url, **kwargs)

    def lookup(self, kind, external_id):
        query = urllib.parse.quote(f'externalId eq "{external_id}"')
        response = self._send("get", f"{BASE}/{kind}?filter={query}")
        if response.status_code > 204:
            return None
        data = response.json()
        return None if data["totalResults"] == 0 else data["Resources"][0]

    def provision(self, kind, body):
        existing = self.lookup(kind, body["externalId"])
        if existing:
            body = {**body, "id": existing["id"]}
            return self._send("put", f"{BASE}{existing['meta']['location']}", body)
        return self._send("post", f"{BASE}/{kind}", body)

    def delete(self, resource):
        return self._send("delete", f"{BASE}{resource['meta']['location']}")

    def list_all(self, kind):
        resources = []
        while True:
            response = self._send(
                "get", f"{BASE}/{kind}?startIndex={len(resources) + 1}"
            )
            assert response.status_code == 200, response.content
            data = response.json()
            resources += data["Resources"]
            if data["totalResults"] == len(resources):
                return resources

    def sweep_delete(self, known_external_ids):
        """The destructive half of perform_sweep: drop what SRAM doesn't know."""
        deleted = []
        for kind in ("Groups", "Users"):
            for resource in self.list_all(kind):
                if resource.get("externalId", "") not in known_external_ids:
                    response = self.delete(resource)
                    deleted.append((kind, resource["id"], response.status_code))
        return deleted


@override_config(SCIM_INBOUND_ENABLED=True, SRAM_INTEGRATION_ENABLED=True)
class SramScimTest(test.APITestCase):
    def setUp(self):
        self.service_account = structure_factories.UserFactory(
            username="scim-sram-svc", is_staff=True
        )
        token, _ = Token.objects.get_or_create(user=self.service_account)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.key}")
        self.sbs = SbsClient(self.client)

    def provision_user(self, **kwargs):
        body = payloads.sram_user(**kwargs)
        response = self.sbs.provision("Users", body)
        self.assertIn(response.status_code, (200, 201), response.content)
        return body, response.json()
