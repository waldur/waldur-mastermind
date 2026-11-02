"""Who may replace or delete an order's purchase order.

``update_attachment`` and ``delete_attachment`` change the purchase order that
``approve_by_consumer`` requires when the offering sets
``require_purchase_order_upload``. Seeing an order is not enough to change it:
the read-only roles and the provider side see the order too. Only those who may
place the order on the consumer side (``ORDER.CREATE`` on its project or
organization) and staff may change it.

The roles hold the permissions ``permissions.yaml`` ships with, so the tests
check the default deployment rather than grants made up for them. The structure
fixtures add list permissions on top (organization support gets ORDER.LIST),
which only widens who sees the order; none of them grants ORDER.CREATE.
"""

from pathlib import Path

import yaml
from ddt import data, ddt
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status, test

from waldur_core.permissions.enums import TYPE_MAP
from waldur_core.permissions.fixtures import (
    CustomerRole,
    ServiceProviderRole,
)
from waldur_core.permissions.models import Role
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.enums import OrderStates
from waldur_mastermind.marketplace.tests import factories, fixtures

PERMISSIONS_YAML = Path(settings.BASE_DIR) / "docker/rootfs/etc/waldur/permissions.yaml"

PDF = (
    b"%PDF-1.4\n1 0 obj\n<<\n/Type /Catalog\n>>\nendobj\n"
    b"xref\n0 1\n0000000000 65535 f \ntrailer\n<<\n/Size 1\n"
    b"/Root 1 0 R\n>>\nstartxref\n9\n%%EOF"
)

ACTIONS = ("update_attachment", "delete_attachment")

# Callers who see the order but may not place it: read-only roles and the
# provider side. They get 403, not 404, because the order is visible to them.
REFUSED = (
    "customer_reader",
    "customer_support",
    "global_support",
    "offering_manager",
    "service_provider_manager",
    "provider_owner",
)

ALLOWED = ("owner", "admin", "manager", "staff")


def grant_default_permissions():
    """Give the system roles the permissions ``permissions.yaml`` ships with."""
    for row in yaml.safe_load(PERMISSIONS_YAML.read_text()):
        if row.get("scope") not in TYPE_MAP:
            continue
        role = Role.objects.get_system_role(
            row["role"],
            content_type=ContentType.objects.get_by_natural_key(
                *TYPE_MAP[row["scope"]]
            ),
        )
        for permission in row.get("permissions", []):
            role.add_permission(permission)


def make_pdf(name):
    return SimpleUploadedFile(name, PDF, content_type="application/pdf")


@ddt
class OrderAttachmentPermissionsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        grant_default_permissions()
        self.order = factories.OrderFactory(
            project=self.fixture.project,
            offering=self.fixture.offering,
            plan=self.fixture.plan,
            resource=self.fixture.resource,
            created_by=self.fixture.manager,
            state=OrderStates.PENDING_CONSUMER,
            attachment=make_pdf("purchase-order.pdf"),
        )
        self.original_attachment = self.order.attachment.name

    def get_user(self, name):
        if name == "customer_reader":
            user = structure_factories.UserFactory()
            self.fixture.customer.add_user(user, CustomerRole.READER)
            return user
        if name == "service_provider_manager":
            user = structure_factories.UserFactory()
            self.fixture.service_provider.add_user(user, ServiceProviderRole.MANAGER)
            return user
        if name == "provider_owner":
            # Owner of the organization that provides the offering.
            return self.fixture.offering_owner
        return getattr(self.fixture, name)

    def post(self, user, action):
        self.client.force_authenticate(user)
        url = factories.OrderFactory.get_url(self.order, action)
        if action == "update_attachment":
            response = self.client.post(
                url,
                {"attachment": make_pdf("replacement.pdf")},
                format="multipart",
            )
        else:
            response = self.client.post(url)
        self.order.refresh_from_db()
        return response

    def assert_attachment_unchanged(self):
        self.assertEqual(self.order.attachment.name, self.original_attachment)
        with self.order.attachment.open("rb") as stored:
            self.assertEqual(stored.read(), PDF)

    @data(*[(role, action) for role in REFUSED for action in ACTIONS])
    def test_caller_who_may_not_place_order_cannot_change_attachment(self, case):
        role, action = case
        user = self.get_user(role)

        response = self.post(user, action)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, role)
        self.assert_attachment_unchanged()

    @data(*ACTIONS)
    def test_unrelated_user_cannot_find_order(self, action):
        response = self.post(structure_factories.UserFactory(), action)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assert_attachment_unchanged()

    @data(*ALLOWED)
    def test_caller_who_may_place_order_can_replace_attachment(self, role):
        response = self.post(self.get_user(role), "update_attachment")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertNotEqual(self.order.attachment.name, self.original_attachment)
        self.assertIn("replacement", self.order.attachment.name)

    @data(*ALLOWED)
    def test_caller_who_may_place_order_can_delete_attachment(self, role):
        response = self.post(self.get_user(role), "delete_attachment")

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(self.order.attachment)

    def test_owner_can_upload_attachment_to_order_without_one(self):
        self.order.attachment = None
        self.order.save(update_fields=["attachment"])

        response = self.post(self.fixture.owner, "update_attachment")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIn("replacement", self.order.attachment.name)

    @data(*ACTIONS)
    def test_attachment_of_finished_order_cannot_be_changed(self, action):
        self.order.state = OrderStates.DONE
        self.order.save(update_fields=["state"])

        response = self.post(self.fixture.manager, action)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assert_attachment_unchanged()
