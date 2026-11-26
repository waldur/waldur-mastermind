"""Secret options on resource requests and call resource templates (#1334).

The resource request wizard reuses the order form, so the value of a secret
option can arrive in a request's or a template's attributes. It is never kept
there: a request, an award and the resource allocated from it carry no secret.
"""

import json

from rest_framework import status, test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.proposal import models, serializers, utils
from waldur_mastermind.proposal.tests import factories
from waldur_mastermind.proposal.tests.test_prepaid_duration import PrepaidFixture

SECRET = "Pr0posal-Secret-Value!"
PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----\n"


def secret(**default_configs):
    return {"type": "secret", "label": "Token", "default_configs": default_configs}


class SecretRequestTest(PrepaidFixture, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.offering.options = {
            "order": ["bucket", "token"],
            "options": {
                "bucket": {"type": "string", "label": "Bucket"},
                "token": secret(multiline=True),
            },
        }
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

    def assert_absent_everywhere(self, *payloads):
        texts = [
            payload if isinstance(payload, str) else json.dumps(payload, default=str)
            for payload in payloads
        ]
        texts += [
            json.dumps(item.attributes)
            for model in (
                models.RequestedResource,
                models.CallResourceTemplate,
                marketplace_models.Order,
                marketplace_models.Resource,
            )
            for item in model.objects.all()
        ]
        for text in texts:
            self.assertNotIn(SECRET, text)
            self.assertNotIn("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", text)

    def test_request_does_not_keep_the_secret(self):
        response = self.create(bucket="data", token=SECRET)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["attributes"], {"bucket": "data"})
        self.assert_absent_everywhere(response.content.decode())

    def test_multiline_secret_is_dropped_too(self):
        response = self.create(bucket="data", token=PEM)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assert_absent_everywhere(response.content.decode())

    def test_invalid_secret_is_refused_without_echoing_it(self):
        response = self.create(bucket="data", token="bad\x07value-secret")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("value-secret", response.content.decode())

    def test_edit_does_not_keep_the_secret(self):
        response = self.create(bucket="data")
        requested_resource = models.RequestedResource.objects.get(
            uuid=response.data["uuid"]
        )
        url = factories.RequestedResourceFactory.get_url(
            self.proposal, requested_resource
        )
        response = self.client.patch(
            url, {"attributes": {"bucket": "logs", "token": SECRET}}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        requested_resource.refresh_from_db()
        self.assertEqual(requested_resource.attributes, {"bucket": "logs"})
        self.assert_absent_everywhere(response.content.decode())

    def test_resource_only_secret_is_dropped_from_an_offering_without_order_options(
        self,
    ):
        self.offering.options = {}
        self.offering.resource_options = {
            "order": ["token"],
            "options": {"token": secret()},
        }
        self.offering.save()
        self.assertEqual(
            utils.normalize_option_values(
                {"bucket": "data", "token": SECRET}, self.offering
            ),
            {"bucket": "data"},
        )

    def test_template_does_not_keep_the_secret(self):
        template = factories.CallResourceTemplateFactory(
            call=self.fixture.call,
            requested_offering=self.requested_offering,
            attributes={},
        )
        serializer = serializers.CallResourceTemplateSerializer(
            instance=template,
            data={"attributes": {"bucket": "data", "token": SECRET}},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["attributes"], {"bucket": "data"})
        serializer.save()
        template.refresh_from_db()
        self.assertEqual(template.attributes, {"bucket": "data"})
        limits, attributes = utils.template_request_values(template)
        self.assertNotIn("token", attributes)
        self.assert_absent_everywhere()
