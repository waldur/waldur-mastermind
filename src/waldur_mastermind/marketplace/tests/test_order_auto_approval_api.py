from decimal import Decimal

from django.urls import reverse
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models

LIST_URL = "http://testserver" + reverse("marketplace-project-order-auto-approval-list")


def detail_url(rule):
    return "http://testserver" + reverse(
        "marketplace-project-order-auto-approval-detail",
        kwargs={"uuid": rule.uuid.hex},
    )


def project_url(project):
    return "http://testserver" + reverse(
        "project-detail", kwargs={"uuid": project.uuid.hex}
    )


class ProjectOrderAutoApprovalApiTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.APPROVE_ORDER)

    def _create_payload(self, **overrides):
        payload = {
            "project": project_url(self.project),
            "monthly_cost_limit": "100",
            "enabled": True,
        }
        payload.update(overrides)
        return payload

    def test_owner_can_create(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(LIST_URL, self._create_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        rule = models.ProjectOrderAutoApproval.objects.get(project=self.project)
        self.assertEqual(rule.created_by, self.fixture.owner)
        self.assertEqual(rule.monthly_cost_limit, Decimal("100"))

    def test_project_manager_with_approve_order_can_create(self):
        self.client.force_authenticate(self.fixture.manager)
        response = self.client.post(LIST_URL, self._create_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_project_member_without_approve_order_cannot_create(self):
        self.client.force_authenticate(self.fixture.member)
        response = self.client.post(LIST_URL, self._create_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_create_without_role(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(LIST_URL, self._create_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_unrelated_user_cannot_see_others_rule(self):
        models.ProjectOrderAutoApproval.objects.create(
            project=self.project,
            monthly_cost_limit=Decimal("50"),
            created_by=self.fixture.owner,
        )
        other = fixtures.ProjectFixture().manager  # member of unrelated project
        self.client.force_authenticate(other)
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_owner_can_update_limit(self):
        rule = models.ProjectOrderAutoApproval.objects.create(
            project=self.project,
            monthly_cost_limit=Decimal("50"),
            created_by=self.fixture.owner,
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(detail_url(rule), {"monthly_cost_limit": "75"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rule.refresh_from_db()
        self.assertEqual(rule.monthly_cost_limit, Decimal("75"))
        self.assertEqual(rule.modified_by, self.fixture.owner)

    def test_owner_can_destroy(self):
        rule = models.ProjectOrderAutoApproval.objects.create(
            project=self.project,
            monthly_cost_limit=Decimal("50"),
            created_by=self.fixture.owner,
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.delete(detail_url(rule))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.ProjectOrderAutoApproval.objects.filter(pk=rule.pk).exists()
        )

    def test_validate_monthly_cost_limit_must_be_positive(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            LIST_URL, self._create_payload(monthly_cost_limit="0")
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("monthly_cost_limit", response.data)

    def test_unique_per_project(self):
        self.client.force_authenticate(self.fixture.owner)
        first = self.client.post(LIST_URL, self._create_payload())
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        second = self.client.post(LIST_URL, self._create_payload())
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)

    def test_blocked_customer_rejects_create(self):
        self.fixture.customer.blocked = True
        self.fixture.customer.save()
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(LIST_URL, self._create_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
