"""Tests for OfferingGroup CRUD, customer-match validation, and offering FK behaviour.

Covers:
- CRUD permissions: staff full access; service-provider customer owner full access
  to their own groups; non-SP owner cannot create; cross-customer access denied.
- Customer-match validation: assigning offering_group with a different customer fails.
- SET_NULL on group delete: offering's offering_group becomes None.
- ?offering_group_uuid filter on the offering list endpoint.
"""

from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.models import Offering
from waldur_mastermind.marketplace.tests import factories


def _seed_offering_permissions():
    """Test DB does not auto-import permissions.yaml — seed the relevant
    OFFERING.* permissions on CUSTOMER.OWNER so that owners can create,
    update and delete OfferingGroups in tests."""
    CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
    CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
    CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_OFFERING)


class OfferingGroupListTest(test.APITestCase):
    """Listing is scoped to customers the user holds a role on (GenericRoleFilter)."""

    def setUp(self):
        _seed_offering_permissions()
        self.fixture_a = structure_fixtures.CustomerFixture()
        self.fixture_b = structure_fixtures.CustomerFixture()
        factories.ServiceProviderFactory(customer=self.fixture_a.customer)
        factories.ServiceProviderFactory(customer=self.fixture_b.customer)

        self.group_a = factories.OfferingGroupFactory(customer=self.fixture_a.customer)
        self.group_b = factories.OfferingGroupFactory(customer=self.fixture_b.customer)
        self.list_url = factories.OfferingGroupFactory.get_list_url()

    def test_staff_sees_all_groups(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertEqual(uuids, {self.group_a.uuid.hex, self.group_b.uuid.hex})

    def test_owner_sees_only_their_customer_groups(self):
        self.client.force_authenticate(self.fixture_a.owner)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertEqual(uuids, {self.group_a.uuid.hex})

    def test_filter_by_customer_uuid(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(
            self.list_url, {"customer_uuid": self.fixture_a.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertEqual(uuids, {self.group_a.uuid.hex})


class OfferingGroupCreateTest(test.APITestCase):
    def setUp(self):
        _seed_offering_permissions()
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.list_url = factories.OfferingGroupFactory.get_list_url()

    def _customer_url(self, customer):
        return structure_factories.CustomerFactory.get_url(customer)

    def _payload(self, customer):
        return {
            "title": "cluster-alpha",
            "description": "All partitions of the alpha cluster",
            "customer": self._customer_url(customer),
        }

    def test_staff_can_create_when_customer_is_service_provider(self):
        factories.ServiceProviderFactory(customer=self.customer)
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.post(self.list_url, self._payload(self.customer))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            models.OfferingGroup.objects.filter(
                title="cluster-alpha", customer=self.customer
            ).exists()
        )

    def test_create_fails_when_customer_is_not_service_provider(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.post(self.list_url, self._payload(self.customer))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("customer", response.data)

    def test_owner_of_service_provider_can_create(self):
        factories.ServiceProviderFactory(customer=self.customer)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.list_url, self._payload(self.customer))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_unrelated_user_cannot_create(self):
        factories.ServiceProviderFactory(customer=self.customer)
        outsider = structure_factories.UserFactory()
        self.client.force_authenticate(outsider)
        response = self.client.post(self.list_url, self._payload(self.customer))
        self.assertIn(
            response.status_code,
            {status.HTTP_403_FORBIDDEN, status.HTTP_400_BAD_REQUEST},
        )


class OfferingGroupUpdateDeleteTest(test.APITestCase):
    def setUp(self):
        _seed_offering_permissions()
        self.fixture_a = structure_fixtures.CustomerFixture()
        self.fixture_b = structure_fixtures.CustomerFixture()
        factories.ServiceProviderFactory(customer=self.fixture_a.customer)
        factories.ServiceProviderFactory(customer=self.fixture_b.customer)
        self.group = factories.OfferingGroupFactory(customer=self.fixture_a.customer)
        self.detail_url = factories.OfferingGroupFactory.get_url(self.group)

    def test_owner_can_update_their_group(self):
        self.client.force_authenticate(self.fixture_a.owner)
        response = self.client.patch(self.detail_url, {"title": "renamed"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.group.refresh_from_db()
        self.assertEqual(self.group.title, "renamed")

    def test_other_owner_cannot_update_group(self):
        self.client.force_authenticate(self.fixture_b.owner)
        response = self.client.patch(self.detail_url, {"title": "stolen"})
        # GenericRoleFilter excludes the row from the queryset for fixture_b
        # so DRF returns 404 rather than 403.
        self.assertIn(
            response.status_code,
            {status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN},
        )

    def test_owner_can_delete_their_group(self):
        self.client.force_authenticate(self.fixture_a.owner)
        response = self.client.delete(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.OfferingGroup.objects.filter(pk=self.group.pk).exists())

    def test_staff_can_update_any_group(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.patch(self.detail_url, {"title": "by-staff"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_customer_field_is_protected_on_update(self):
        self.client.force_authenticate(self.fixture_a.owner)
        new_customer_url = structure_factories.CustomerFactory.get_url(
            self.fixture_b.customer
        )
        response = self.client.patch(self.detail_url, {"customer": new_customer_url})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.group.refresh_from_db()
        # protected_fields prevents reassignment after creation
        self.assertEqual(self.group.customer_id, self.fixture_a.customer.id)


class OfferingFKBehaviourTest(test.APITestCase):
    """Behaviour of Offering.offering_group: SET_NULL on delete + filter."""

    def setUp(self):
        _seed_offering_permissions()
        self.fixture = structure_fixtures.CustomerFixture()
        factories.ServiceProviderFactory(customer=self.fixture.customer)
        self.group = factories.OfferingGroupFactory(customer=self.fixture.customer)
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer, offering_group=self.group
        )

    def test_offering_group_is_cleared_when_group_deleted(self):
        self.group.delete()
        self.offering.refresh_from_db()
        self.assertIsNone(self.offering.offering_group)

    def test_filter_offerings_by_offering_group_uuid(self):
        # Add another offering NOT in the group
        other = factories.OfferingFactory(customer=self.fixture.customer)

        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url, {"offering_group_uuid": self.group.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertIn(self.offering.uuid.hex, uuids)
        self.assertNotIn(other.uuid.hex, uuids)


class ProviderOfferingResponseTest(test.APITestCase):
    """The provider-offering detail/list endpoint must expose the new
    ``offering_group`` / ``offering_group_uuid`` / ``offering_group_title``
    read-only fields without breaking the existing response shape.
    """

    def setUp(self):
        _seed_offering_permissions()
        self.fixture = structure_fixtures.CustomerFixture()
        factories.ServiceProviderFactory(customer=self.fixture.customer)
        self.group = factories.OfferingGroupFactory(
            customer=self.fixture.customer, title="cluster-alpha"
        )
        self.offering_with_group = factories.OfferingFactory(
            customer=self.fixture.customer, offering_group=self.group
        )
        self.offering_without_group = factories.OfferingFactory(
            customer=self.fixture.customer
        )

    def _detail_url(self, offering):
        return factories.OfferingFactory.get_url(offering)

    def test_detail_exposes_group_fields_when_assigned(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self._detail_url(self.offering_with_group))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("offering_group", response.data)
        self.assertIn("offering_group_uuid", response.data)
        self.assertIn("offering_group_title", response.data)
        self.assertEqual(
            str(response.data["offering_group_uuid"]), str(self.group.uuid)
        )
        self.assertEqual(response.data["offering_group_title"], "cluster-alpha")
        self.assertIn(
            f"marketplace-offering-groups/{self.group.uuid.hex}",
            response.data["offering_group"],
        )

    def test_detail_exposes_null_group_fields_when_unassigned(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self._detail_url(self.offering_without_group))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["offering_group"])
        self.assertIsNone(response.data["offering_group_uuid"])
        self.assertIsNone(response.data["offering_group_title"])

    def test_list_includes_group_fields(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(factories.OfferingFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = next(
            r for r in response.data if r["uuid"] == self.offering_with_group.uuid.hex
        )
        self.assertEqual(row["offering_group_title"], "cluster-alpha")

    def test_existing_offering_fields_still_present(self):
        """Adding offering_group must not strip any existing field from the
        offering detail response."""
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self._detail_url(self.offering_with_group))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # A representative subset — covers identity, customer block, profile
        # block (which we sit alongside), and the wider response shape.
        for key in (
            "uuid",
            "name",
            "slug",
            "customer",
            "customer_uuid",
            "customer_name",
            "category",
            "type",
            "state",
            "profile_uuid",
            "profile_name",
            "plans",
            "components",
        ):
            self.assertIn(key, response.data, f"existing field {key} missing")

    def test_service_provider_scoped_offerings_endpoint_exposes_group_fields(
        self,
    ):
        """The slim list at /api/marketplace-service-providers/{uuid}/offerings/
        must also surface offering_group_uuid/title so frontend row-actions
        can pre-populate the current group without an extra fetch."""
        from rest_framework.reverse import reverse

        provider = models.ServiceProvider.objects.get(customer=self.fixture.customer)
        url = reverse(
            "service-provider-offerings-list",
            kwargs={"service_provider_uuid": provider.uuid.hex},
        )
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = next(
            r for r in response.data if r["uuid"] == self.offering_with_group.uuid.hex
        )
        self.assertEqual(str(row["offering_group_uuid"]), str(self.group.uuid))
        self.assertEqual(row["offering_group_title"], "cluster-alpha")

        empty_row = next(
            r
            for r in response.data
            if r["uuid"] == self.offering_without_group.uuid.hex
        )
        self.assertIsNone(empty_row["offering_group_uuid"])
        self.assertIsNone(empty_row["offering_group_title"])


class OfferingCreateWithGroupTest(test.APITestCase):
    """``offering_group`` is writable on POST /marketplace-provider-offerings/.

    Covers the additive path: a service-provider owner can create an offering
    with a group set (when group's customer matches), receives 400 when it
    does not match, and creating without the field still works (regression
    guard for existing clients that do not send the new field).
    """

    def setUp(self):
        _seed_offering_permissions()
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        factories.ServiceProviderFactory(customer=self.customer)
        self.category = factories.CategoryFactory()
        self.list_url = factories.OfferingFactory.get_list_url()

    def _payload(self, **overrides):
        payload = {
            "name": "Owned offering",
            "type": "Support.OfferingTemplate",
            "category": factories.CategoryFactory.get_url(self.category),
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
        }
        payload.update(overrides)
        return payload

    def test_create_with_same_customer_group_succeeds(self):
        group = factories.OfferingGroupFactory(customer=self.customer)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            self.list_url,
            self._payload(offering_group=group.uuid.hex),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(offering.offering_group, group)

    def test_create_with_foreign_customer_group_rejected(self):
        other_customer = structure_factories.CustomerFactory()
        factories.ServiceProviderFactory(customer=other_customer)
        foreign_group = factories.OfferingGroupFactory(customer=other_customer)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            self.list_url,
            self._payload(offering_group=foreign_group.uuid.hex),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("offering_group", response.data)
        self.assertFalse(
            models.Offering.objects.filter(offering_group=foreign_group).exists()
        )

    def test_create_without_offering_group_still_succeeds(self):
        """Regression guard: clients that do not send the new field must
        keep working — offering created with offering_group=None."""
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.list_url, self._payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertIsNone(offering.offering_group)


class SetOfferingGroupActionTest(test.APITestCase):
    """The ``set_offering_group`` action on ProviderOfferingViewSet."""

    def setUp(self):
        _seed_offering_permissions()
        self.fixture = structure_fixtures.CustomerFixture()
        factories.ServiceProviderFactory(customer=self.fixture.customer)
        # ``can_update_offering`` only allows non-staff to mutate offerings
        # in DRAFT state — match the behaviour of the regular update action.
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer, state=Offering.States.DRAFT
        )
        self.group = factories.OfferingGroupFactory(customer=self.fixture.customer)
        self.url = factories.OfferingFactory.get_url(
            self.offering, action="set_offering_group"
        )

    def test_owner_assigns_group(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, {"offering_group": self.group.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.offering_group, self.group)

    def test_owner_clears_group(self):
        self.offering.offering_group = self.group
        self.offering.save(update_fields=["offering_group"])
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, {"offering_group": None}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertIsNone(self.offering.offering_group)

    def test_cross_customer_assignment_rejected(self):
        other_fixture = structure_fixtures.CustomerFixture()
        factories.ServiceProviderFactory(customer=other_fixture.customer)
        foreign_group = factories.OfferingGroupFactory(customer=other_fixture.customer)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(
            self.url, {"offering_group": foreign_group.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("offering_group", response.data)

    def test_unrelated_user_cannot_set_group(self):
        outsider = structure_factories.UserFactory()
        self.client.force_authenticate(outsider)
        response = self.client.post(self.url, {"offering_group": self.group.uuid.hex})
        self.assertIn(
            response.status_code,
            {status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND},
        )
