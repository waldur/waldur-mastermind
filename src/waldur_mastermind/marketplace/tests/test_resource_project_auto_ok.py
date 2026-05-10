"""auto_ok_resource_projects offering plugin option.

When True, ResourceProjectSerializer.create() immediately transitions
the new instance from CREATING to OK on save, bypassing the
provider/site-agent reconciliation callback.
"""

from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import ResourceStates
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


def _list_url():
    return "http://testserver" + reverse("marketplace-resource-project-list")


class AutoOkResourceProjectTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.plugin_options = {"enable_resource_projects": True}
        self.offering.save(update_fields=["plugin_options"])
        self.resource = self.fixture.resource
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

    def _create(self, name="rp-1"):
        return self.client.post(
            _list_url(),
            {"resource": self.resource.uuid.hex, "name": name},
            format="json",
        )

    def test_default_state_is_creating_when_option_off(self):
        response = self._create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        rp = models.ResourceProject.objects.get(uuid=response.data["uuid"])
        self.assertEqual(rp.state, ResourceStates.CREATING)

    def test_state_becomes_ok_when_option_on(self):
        self.offering.plugin_options["auto_ok_resource_projects"] = True
        self.offering.save(update_fields=["plugin_options"])

        response = self._create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        rp = models.ResourceProject.objects.get(uuid=response.data["uuid"])
        self.assertEqual(rp.state, ResourceStates.OK)

    def test_option_off_explicitly_does_not_auto_ok(self):
        self.offering.plugin_options["auto_ok_resource_projects"] = False
        self.offering.save(update_fields=["plugin_options"])

        response = self._create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        rp = models.ResourceProject.objects.get(uuid=response.data["uuid"])
        self.assertEqual(rp.state, ResourceStates.CREATING)
