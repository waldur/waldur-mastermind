from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_autoprovisioning import models
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories


class RuleCreatePermissionTest(test.APITestCase):
    """Creating an autoprovisioning rule is staff-only.

    A rule grants a project role on its customer to every future user matching
    its email patterns, and provisions an order against that customer when a
    plan is set. Before the fix the viewset guarded update/destroy/test-match
    but not create, so any authenticated user could bind a rule to an
    organization they had no relationship with.
    """

    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.owner = structure_factories.UserFactory()
        self.customer.add_user(self.owner, CustomerRole.OWNER)
        self.outsider = structure_factories.UserFactory()

    def _url(self):
        return "http://testserver" + reverse("autoprovisioning-rule-list")

    def _payload(self):
        return {
            "name": "rule",
            "customer": "http://testserver"
            + reverse("customer-detail", kwargs={"uuid": self.customer.uuid.hex}),
            "project_role_name": ProjectRole.ADMIN.name,
            "user_email_patterns": [r".+@example\.com"],
        }

    def test_staff_can_create_rule(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(self._url(), self._payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.Rule.objects.count(), 1)

    def test_customer_owner_cannot_create_rule(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post(self._url(), self._payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(models.Rule.objects.count(), 0)

    def test_unrelated_user_cannot_create_rule_for_arbitrary_customer(self):
        self.client.force_authenticate(self.outsider)
        response = self.client.post(self._url(), self._payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(models.Rule.objects.count(), 0)

    def test_anonymous_user_cannot_create_rule(self):
        response = self.client.post(self._url(), self._payload())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(models.Rule.objects.count(), 0)
