"""OfferingProfile (service-profile) tests.

Cover the sync behaviour: when staff edits the profile's role catalog OR
when a service provider binds an offering to a profile, RoleAvailability
rows on the affected offerings are reconciled by Celery tasks.

Tests run tasks eagerly via CELERY_TASK_ALWAYS_EAGER (Waldur test settings).
"""

from django.contrib.contenttypes.models import ContentType
from django.test import override_settings
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.permissions.models import Role, RoleAvailability, UserRole
from waldur_core.permissions.utils import add_user
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


def _profile_url(profile=None, action=None):
    if profile is None:
        return "http://testserver" + reverse("marketplace-offering-profile-list")
    base = "http://testserver" + reverse(
        "marketplace-offering-profile-detail", kwargs={"uuid": profile.uuid.hex}
    )
    return f"{base}{action}/" if action else base


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class OfferingProfileSyncTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.regular = structure_factories.UserFactory()

        # Two offerings + one profile
        self.offering_a = marketplace_factories.OfferingFactory(name="Offering A")
        self.offering_b = marketplace_factories.OfferingFactory(name="Offering B")
        self.profile = models.OfferingProfile.objects.create(
            name="rancher", description="Rancher cluster catalog"
        )

        # One role pre-existing in the profile catalog
        rp_ct = ContentType.objects.get_for_model(models.ResourceProject)
        self.role = Role.objects.create(
            name="ns_manager",
            content_type=rp_ct,
            is_system_role=False,
        )
        self.profile.roles.add(self.role)

        self.offering_ct = ContentType.objects.get_for_model(models.Offering)

    def test_binding_offering_seeds_role_availability(self):
        """Setting offering.profile auto-creates RoleAvailability rows."""
        with self.captureOnCommitCallbacks(execute=True):
            self.offering_a.profile = self.profile
            self.offering_a.save()

        self.assertTrue(
            RoleAvailability.objects.filter(
                role=self.role,
                content_type=self.offering_ct,
                object_id=self.offering_a.id,
            ).exists()
        )

    def test_unbinding_offering_removes_profile_seeded_rows(self):
        """Setting offering.profile=None removes profile-seeded rows."""
        with self.captureOnCommitCallbacks(execute=True):
            self.offering_a.profile = self.profile
            self.offering_a.save()
        self.assertTrue(
            RoleAvailability.objects.filter(
                role=self.role, object_id=self.offering_a.id
            ).exists()
        )

        with self.captureOnCommitCallbacks(execute=True):
            self.offering_a.profile = None
            self.offering_a.save()
        self.assertFalse(
            RoleAvailability.objects.filter(
                role=self.role, object_id=self.offering_a.id
            ).exists()
        )

    def test_adding_role_to_profile_seeds_bound_offerings(self):
        """When staff adds a role to the profile, bound offerings get it."""
        with self.captureOnCommitCallbacks(execute=True):
            self.offering_a.profile = self.profile
            self.offering_a.save()
            self.offering_b.profile = self.profile
            self.offering_b.save()

        new_role = Role.objects.create(
            name="cluster_admin",
            content_type=ContentType.objects.get_for_model(models.Resource),
            is_system_role=False,
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.profile.roles.add(new_role)

        for offering in (self.offering_a, self.offering_b):
            self.assertTrue(
                RoleAvailability.objects.filter(
                    role=new_role, object_id=offering.id
                ).exists(),
                f"Expected RoleAvailability on {offering.name}",
            )

    def test_removing_role_from_profile_revokes_user_grants(self):
        """When staff removes a role from the profile, dependent UserRoles
        on bound offerings get revoked via the existing cascade signal."""
        with self.captureOnCommitCallbacks(execute=True):
            self.offering_a.profile = self.profile
            self.offering_a.save()

        # Create a Resource + ResourceProject + grant a UserRole that the
        # availability allows.
        resource = marketplace_factories.ResourceFactory(offering=self.offering_a)
        rp = models.ResourceProject.objects.create(resource=resource, name="P1")
        user = structure_factories.UserFactory()
        add_user(rp, user, self.role)

        ur = UserRole.objects.get(user=user, role=self.role)
        self.assertTrue(ur.is_active)

        # Remove role from profile — sync should drop RoleAvailability,
        # which cascade-revokes the UserRole.
        with self.captureOnCommitCallbacks(execute=True):
            self.profile.roles.remove(self.role)

        ur.refresh_from_db()
        self.assertFalse(ur.is_active)


class OfferingProfileViewSetTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.regular = structure_factories.UserFactory()
        self.profile = models.OfferingProfile.objects.create(
            name="rancher", description="Catalog"
        )
        self.role = Role.objects.create(
            name="cluster_admin",
            content_type=ContentType.objects.get_for_model(models.Resource),
            is_system_role=False,
        )

    def test_staff_can_create_profile(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            _profile_url(),
            {"name": "slurm", "description": "SLURM allocations"},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.OfferingProfile.objects.filter(name="slurm").exists())

    def test_regular_user_cannot_create_profile(self):
        self.client.force_authenticate(self.regular)
        response = self.client.post(
            _profile_url(), {"name": "slurm", "description": ""}
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_400_BAD_REQUEST),
        )

    def test_staff_can_add_role(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            _profile_url(self.profile, "add_role"),
            {"role": self.role.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(self.role, self.profile.roles.all())

    def test_staff_can_remove_role(self):
        self.profile.roles.add(self.role)
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            _profile_url(self.profile, "remove_role"),
            {"role": self.role.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(self.role, self.profile.roles.all())

    def test_regular_user_cannot_add_role(self):
        self.client.force_authenticate(self.regular)
        response = self.client.post(
            _profile_url(self.profile, "add_role"),
            {"role": self.role.uuid.hex},
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class OfferingRoleApiScopingTest(test.APITestCase):
    """marketplace-offering-roles endpoint must scope by offering, and on
    profile-bound offerings must derive its catalog from the profile —
    not from any direct RoleAvailability rows that may have been created
    before the offering was bound."""

    def setUp(self):
        from waldur_core.structure.tests import fixtures as structure_fixtures

        self.staff = structure_factories.UserFactory(is_staff=True)
        self.profile = models.OfferingProfile.objects.create(
            name="rancher", description=""
        )
        rp_ct = ContentType.objects.get_for_model(models.ResourceProject)
        self.profile_role = Role.objects.create(
            name="profile_role", content_type=rp_ct, is_system_role=False
        )
        self.profile.roles.add(self.profile_role)

        sf = structure_fixtures.CustomerFixture()
        self.customer = sf.customer
        self.owner = sf.owner

        self.bound_offering = marketplace_factories.OfferingFactory(
            name="Bound", customer=self.customer
        )
        self.unbound_offering = marketplace_factories.OfferingFactory(
            name="Unbound", customer=self.customer
        )
        self.bound_offering.profile = self.profile
        self.bound_offering.save()

    def _list(self, offering_uuid):
        url = "http://testserver" + reverse("marketplace-offering-role-list")
        self.client.force_authenticate(self.staff)
        response = self.client.get(url, {"offering_uuid": offering_uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [r["name"] for r in response.data]

    def test_profile_bound_offering_returns_only_profile_roles(self):
        # Inject a stale RoleAvailability binding directly to the offering.
        rp_ct = ContentType.objects.get_for_model(models.ResourceProject)
        stale = Role.objects.create(
            name="stale_role", content_type=rp_ct, is_system_role=False
        )
        offering_ct = ContentType.objects.get_for_model(models.Offering)
        RoleAvailability.objects.create(
            role=stale,
            content_type=offering_ct,
            object_id=self.bound_offering.id,
        )
        names = self._list(self.bound_offering.uuid)
        self.assertIn("profile_role", names)
        self.assertNotIn("stale_role", names)

    def test_unbound_offering_returns_direct_role_availabilities(self):
        rp_ct = ContentType.objects.get_for_model(models.ResourceProject)
        direct = Role.objects.create(
            name="direct_role", content_type=rp_ct, is_system_role=False
        )
        offering_ct = ContentType.objects.get_for_model(models.Offering)
        RoleAvailability.objects.create(
            role=direct,
            content_type=offering_ct,
            object_id=self.unbound_offering.id,
        )
        names = self._list(self.unbound_offering.uuid)
        self.assertIn("direct_role", names)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class OfferingRoleWriteOnProfileBoundOfferingTest(test.APITestCase):
    """Direct create / update / delete of a role on a profile-bound offering
    must be rejected — the catalog is owned by the profile."""

    def setUp(self):
        from waldur_core.structure.tests import fixtures as structure_fixtures

        self.staff = structure_factories.UserFactory(is_staff=True)
        self.profile = models.OfferingProfile.objects.create(
            name="rancher", description=""
        )
        rp_ct = ContentType.objects.get_for_model(models.ResourceProject)
        self.profile_role = Role.objects.create(
            name="profile_role", content_type=rp_ct, is_system_role=False
        )
        self.profile.roles.add(self.profile_role)

        sf = structure_fixtures.CustomerFixture()
        self.customer = sf.customer
        self.owner = sf.owner
        self.bound_offering = marketplace_factories.OfferingFactory(
            name="Bound", customer=self.customer
        )
        self.bound_offering.profile = self.profile
        self.bound_offering.save()

        self.unbound_offering = marketplace_factories.OfferingFactory(
            name="Unbound",
            customer=self.customer,
            plugin_options={"enable_resource_projects": True},
        )

    def _create_role_url(self):
        return "http://testserver" + reverse("marketplace-offering-role-list")

    def test_create_rejected_on_profile_bound_offering(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self._create_role_url(),
            {
                "name": "new_role",
                "description": "",
                "content_type_input": "resource_project",
                "offering": self.bound_offering.uuid.hex,
            },
            format="json",
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN),
        )

    def test_create_allowed_on_unbound_offering(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self._create_role_url(),
            {
                "name": "new_role",
                "description": "",
                "content_type_input": "resource_project",
                "offering": self.unbound_offering.uuid.hex,
            },
            format="json",
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_201_CREATED,
            getattr(response, "data", response.content),
        )


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class OfferingProfileBindingIsStaffOnlyTest(test.APITestCase):
    """Binding an OfferingProfile to an offering must go through the
    dedicated ``set_profile`` action — it is NOT writable from the offering
    create / update payload. The action itself accepts any caller with
    UPDATE_OFFERING on the offering's customer (service-provider owners
    and staff)."""

    def setUp(self):
        from waldur_core.permissions.enums import PermissionEnum
        from waldur_core.permissions.fixtures import CustomerRole as CR
        from waldur_core.structure.tests import factories as sf

        # Ensure CUSTOMER.OWNER actually carries OFFERING.UPDATE in this
        # test process — the test DB does not always have permissions.yaml
        # imported.
        CR.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)

        self.staff = structure_factories.UserFactory(is_staff=True)
        self.owner = structure_factories.UserFactory()
        self.customer = sf.CustomerFactory()
        # Promote owner to CUSTOMER.OWNER on their org.
        self.customer.add_user(self.owner, CR.OWNER)
        # Make this customer a service provider.
        self.service_provider = marketplace_factories.ServiceProviderFactory(
            customer=self.customer
        )

        self.profile = models.OfferingProfile.objects.create(
            name="rancher", description=""
        )
        rp_ct = ContentType.objects.get_for_model(models.ResourceProject)
        self.profile_role = Role.objects.create(
            name="profile_role", content_type=rp_ct, is_system_role=False
        )
        self.profile.roles.add(self.profile_role)

        self.category = marketplace_factories.CategoryFactory()

    def test_owner_cannot_attach_profile_via_create(self):
        url = "http://testserver" + reverse("marketplace-provider-offering-list")
        self.client.force_authenticate(self.owner)
        self.client.post(
            url,
            {
                "name": "Owned offering",
                "type": "Support.OfferingTemplate",
                "category": marketplace_factories.CategoryFactory.get_url(
                    self.category
                ),
                "customer": "http://testserver"
                + reverse(
                    "customer-detail",
                    kwargs={"uuid": self.customer.uuid.hex},
                ),
                "profile": self.profile.uuid.hex,
            },
            format="json",
        )
        # Either the create succeeds and ignores the profile field, or the
        # serializer rejects it. The critical assertion is that no offering
        # is created with profile != None.
        offerings_with_profile = models.Offering.objects.filter(
            customer=self.customer, profile=self.profile
        )
        self.assertFalse(
            offerings_with_profile.exists(),
            "Profile binding via the offering create endpoint must be ignored; "
            "the only mutation path is the dedicated set_profile action.",
        )

    def test_owner_can_set_profile_on_their_own_offering(self):
        offering = marketplace_factories.OfferingFactory(
            name="Owned offering", customer=self.customer
        )
        url = "http://testserver" + reverse(
            "marketplace-provider-offering-detail",
            kwargs={"uuid": offering.uuid.hex},
        )
        self.client.force_authenticate(self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"{url}set_profile/",
                {"profile": self.profile.uuid.hex},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        offering.refresh_from_db()
        self.assertEqual(offering.profile_id, self.profile.id)

    def test_outsider_cannot_set_profile(self):
        from waldur_core.structure.tests import factories as sf

        outsider = structure_factories.UserFactory()
        offering = marketplace_factories.OfferingFactory(
            name="Owned offering", customer=self.customer
        )
        # outsider has no role on self.customer
        unrelated_customer = sf.CustomerFactory()
        from waldur_core.permissions.fixtures import CustomerRole as CR

        unrelated_customer.add_user(outsider, CR.OWNER)
        url = "http://testserver" + reverse(
            "marketplace-provider-offering-detail",
            kwargs={"uuid": offering.uuid.hex},
        )
        self.client.force_authenticate(outsider)
        response = self.client.post(
            f"{url}set_profile/",
            {"profile": self.profile.uuid.hex},
            format="json",
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        offering.refresh_from_db()
        self.assertIsNone(offering.profile_id)
