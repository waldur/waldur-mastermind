"""Offering option transforms on resource requests, awards and allocation.

The proposal's resource request uses the order form, which previews the value
the transform produces, so the request, the award and the resource allocated
from it store that value, as an order would.
"""

from rest_framework import status, test

from waldur_mastermind.proposal import utils
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_awarded_resources import (
    AwardedResourcesBase,
)
from waldur_mastermind.proposal.tests.test_prepaid_duration import PrepaidFixture

OPTIONS = {
    "order": ["bucket"],
    "options": {
        "bucket": {
            "type": "string",
            "label": "Bucket",
            "required": True,
            "pattern": "[A-Za-z]+",
            "transform": {"prefix": "acme-", "suffix": "-prod", "case": "lower"},
        },
    },
}


class RequestedResourceTransformTest(PrepaidFixture, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.offering.options = OPTIONS
        self.offering.save()
        self.url = factories.RequestedResourceFactory.get_list_url(self.proposal)
        self.client.force_authenticate(self.fixture.proposal_creator)

    def create(self, **attributes):
        return self.client.post(
            self.url,
            {
                "requested_offering_uuid": self.requested_offering.uuid.hex,
                "attributes": attributes,
                "limits": {"cpu_hours": 10},
            },
            format="json",
        )

    def test_requested_value_is_stored_transformed(self):
        response = self.create(bucket="Backups")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["attributes"], {"bucket": "acme-backups-prod"})

    def test_requested_value_is_checked_against_the_pattern(self):
        response = self.create(bucket="back-ups")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("bucket", response.data["attributes"])

    def test_required_option_may_be_left_for_later(self):
        response = self.create()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_edited_value_is_stored_transformed(self):
        requested_resource = self._request(None, bucket="acme-archive-prod")
        url = factories.RequestedResourceFactory.get_url(
            self.proposal, requested_resource
        )

        response = self.client.patch(
            url, {"attributes": {"bucket": "Logs"}}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        requested_resource.refresh_from_db()
        self.assertEqual(requested_resource.attributes, {"bucket": "acme-logs-prod"})

    def test_allocation_transforms_a_value_requested_before_the_transform(self):
        requested_resource = self._request(None, bucket="Legacy")

        resource = self._allocate(requested_resource)

        self.assertEqual(resource.attributes["bucket"], "acme-legacy-prod")
        order = resource.order_set.get()
        self.assertEqual(order.attributes["bucket"], "acme-legacy-prod")

    def test_allocation_keeps_a_transformed_value(self):
        requested_resource = self._request(None, bucket="acme-archive-prod")

        resource = self._allocate(requested_resource)

        self.assertEqual(resource.attributes["bucket"], "acme-archive-prod")


class AwardedResourceTransformTest(AwardedResourcesBase):
    def setUp(self):
        super().setUp()
        self.offering.options = OPTIONS
        self.offering.save()
        self._start_allocation()
        self.awarded = self._awarded()

    def _patch_json(self, payload):
        self.client.force_authenticate(self.call_manager)
        return self.client.patch(self._detail_url(self.awarded), payload, format="json")

    def test_awarded_value_is_stored_transformed(self):
        response = self._patch_json({"attributes": {"bucket": "Metrics"}})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.awarded.refresh_from_db()
        self.assertEqual(self.awarded.attributes, {"bucket": "acme-metrics-prod"})

    def test_awarded_value_is_checked_against_the_pattern(self):
        response = self._patch_json({"attributes": {"bucket": "met-rics"}})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("bucket", response.data["attributes"])

    def test_allocation_stores_the_awarded_value(self):
        self._patch_json({"attributes": {"bucket": "Metrics"}})

        utils.allocate_proposal(self.proposal, approved_by=self.fixture.staff)

        self.requested_resource.refresh_from_db()
        self.assertEqual(
            self.requested_resource.resource.attributes["bucket"], "acme-metrics-prod"
        )
