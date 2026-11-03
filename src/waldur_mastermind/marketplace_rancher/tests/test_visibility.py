from django.urls import reverse
from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import RANCHER_OFFERING
from waldur_mastermind.marketplace.tests import factories as marketplace_factories

ALLOWED_IP = "100.100.100.100"
DENIED_IP = "203.0.113.7"


class ManagedRancherResourceVisibilityTest(test.APITestCase):
    """The managed Rancher routes show a resource to whom the consumer
    resource routes show it."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.resource = marketplace_factories.ResourceFactory(
            offering=marketplace_factories.OfferingFactory(type=RANCHER_OFFERING),
            project=self.fixture.project,
        )
        self.list_url = reverse("managed-rancher-cluster-resource-list")
        self.detail_url = reverse(
            "managed-rancher-cluster-resource-detail",
            kwargs={"uuid": self.resource.uuid.hex},
        )

    def _listed(self, user, **headers):
        self.client.force_authenticate(user)
        response = self.client.get(self.list_url, **headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [row["uuid"] for row in response.json()]

    def _assert_hidden(self, user, **headers):
        self.assertEqual(self._listed(user, **headers), [])
        self.assertEqual(
            self.client.get(self.detail_url, **headers).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.post(self.detail_url + "add_node/", {}, **headers).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_user_without_a_role_sees_nothing(self):
        self._assert_hidden(structure_factories.UserFactory())

    def test_owner_of_another_organization_sees_nothing(self):
        self._assert_hidden(structure_fixtures.ProjectFixture().owner)

    def test_owner_of_the_providing_organization_sees_nothing(self):
        # The provider's view of its resources is /api/marketplace-provider-resources/.
        provider_owner = structure_factories.UserFactory()
        provider = self.resource.offering.customer
        assert provider is not None
        provider.add_user(provider_owner, CustomerRole.OWNER)

        self._assert_hidden(provider_owner)

    def test_resource_concealed_outside_the_organizations_networks(self):
        offering = self.resource.offering
        offering.plugin_options = {"conceal_subnet_restricted_resources": True}
        offering.save()
        subnet = structure_models.AccessSubnet.objects.create(
            customer=self.fixture.customer, inet=f"{ALLOWED_IP}/32"
        )
        marketplace_models.AccessSubnetOfferingScope.objects.create(
            access_subnet=subnet, offering=offering
        )
        self.assertEqual(
            self._listed(self.fixture.member, HTTP_X_FORWARDED_FOR=DENIED_IP), []
        )
        self.assertEqual(
            self.client.get(
                self.detail_url, HTTP_X_FORWARDED_FOR=DENIED_IP
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self._listed(self.fixture.member, HTTP_X_FORWARDED_FOR=ALLOWED_IP),
            [self.resource.uuid.hex],
        )
        self.assertEqual(
            self._listed(self.fixture.staff, HTTP_X_FORWARDED_FOR=DENIED_IP),
            [self.resource.uuid.hex],
        )

    def test_consumer_roles_staff_and_support_see_the_resource(self):
        for user in (
            self.fixture.owner,
            self.fixture.admin,
            self.fixture.member,
            self.fixture.staff,
            self.fixture.global_support,
        ):
            with self.subTest(user=user):
                self.assertEqual(self._listed(user), [self.resource.uuid.hex])
                self.assertEqual(
                    self.client.get(self.detail_url).status_code, status.HTTP_200_OK
                )
