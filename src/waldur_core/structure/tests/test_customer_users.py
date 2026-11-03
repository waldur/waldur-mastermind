"""`GET /api/customers/{uuid}/users/` lists an organization's users.

An organization the URL does not name is not found, whatever the caller may
do elsewhere.
"""

import uuid

from django.urls import reverse
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.tests.pat_utils import SUPPORT, use_pat
from waldur_core.structure.tests import factories


def customer_users_url(customer_uuid):
    return "http://testserver" + reverse(
        "customer-users-list", kwargs={"customer_uuid": customer_uuid}
    )


class CustomerUsersTest(test.APITestCase):
    def setUp(self):
        # Roles start bare in tests: grant what the default owner role holds.
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_CUSTOMER_USERS)

        self.customer = factories.CustomerFactory()
        self.owner = factories.UserFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)
        self.member = factories.UserFactory()
        project = factories.ProjectFactory(customer=self.customer)
        project.add_user(self.member, ProjectRole.MEMBER)
        self.staff = factories.UserFactory(is_staff=True)

    def test_owner_lists_the_organization_users(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(customer_users_url(self.customer.uuid.hex))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {user["uuid"] for user in response.data},
            {self.owner.uuid.hex, self.member.uuid.hex},
        )

    def test_caller_without_the_permission_is_refused(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(customer_users_url(self.customer.uuid.hex))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unknown_organization_is_not_found(self):
        for user in (self.owner, self.staff):
            self.client.force_authenticate(user)
            response = self.client.get(customer_users_url(uuid.uuid4().hex))
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_malformed_uuid_is_not_found(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(customer_users_url("not-a-uuid"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@override_config(PAT_ENABLED=True)
class CustomerUsersTokenTest(test.APITestCase):
    """A token's bindings reach this list as they reach the organization's
    own ``list_users``."""

    def setUp(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_CUSTOMER_USERS)
        self.customer = factories.CustomerFactory()
        self.customer.add_user(factories.UserFactory(), CustomerRole.OWNER)
        self.elsewhere = factories.CustomerFactory()
        self.support = factories.UserFactory(is_support=True)

    def get(self, *bindings):
        use_pat(self.client, self.support, SUPPORT, bindings=bindings)
        return self.client.get(customer_users_url(self.customer.uuid.hex))

    def test_support_token_bound_to_the_organization_lists_it(self):
        self.assertEqual(self.get(self.customer).status_code, status.HTTP_200_OK)

    def test_unbound_support_token_lists_it(self):
        self.assertEqual(self.get().status_code, status.HTTP_200_OK)

    def test_support_token_bound_elsewhere_does_not_find_it(self):
        self.assertEqual(
            self.get(self.elsewhere).status_code, status.HTTP_404_NOT_FOUND
        )
