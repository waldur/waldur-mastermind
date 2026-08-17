from unittest import mock

from ddt import data, ddt
from neutronclient.common import exceptions as neutron_exceptions
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_openstack import models

from . import factories, fixtures


@ddt
class CreateRbacPolicyTest(test.APITestCase):
    def setUp(self):
        self.neutron_client_patcher = mock.patch(
            "waldur_openstack.backend.get_neutron_client"
        )
        self.mock_neutron_client = self.neutron_client_patcher.start()
        self.mock_neutron_client().create_rbac_policy.return_value = {
            "rbac_policy": {"id": 1}
        }

        self.keystone_session_patcher = mock.patch(
            "waldur_openstack.backend.get_keystone_session"
        )
        self.mock_keystone_session = self.keystone_session_patcher.start()

        self.fixture = fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.network = self.fixture.network
        self.target_tenant = factories.TenantFactory(
            service_settings=self.tenant.service_settings, backend_id="other_backend_id"
        )
        self.url = "/api/openstack-network-rbac-policies/"

    def tearDown(self):
        self.neutron_client_patcher.stop()
        super().tearDown()

    @data("staff", "owner", "admin", "manager")
    def test_authorized_user_can_create_rbac_policy(self, user):
        self.target_tenant.project.add_user(self.fixture.owner, ProjectRole.ADMIN)
        self.target_tenant.project.add_user(self.fixture.admin, ProjectRole.ADMIN)
        self.target_tenant.project.add_user(self.fixture.manager, ProjectRole.MANAGER)

        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {
            "network": factories.NetworkFactory.get_url(self.network),
            "target_tenant": factories.TenantFactory.get_url(self.target_tenant),
            "policy_type": models.NetworkRBACPolicy.NetworkShareType.SHARED,
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.NetworkRBACPolicy.objects.filter(
                network=self.network,
                target_tenant=self.target_tenant,
                policy_type=models.NetworkRBACPolicy.NetworkShareType.SHARED,
            ).exists()
        )

    @data("owner", "admin", "manager")
    def test_user_without_target_tenant_permissions_cannot_create_rbac_policy(
        self, user
    ):
        self.client.force_authenticate(getattr(self.fixture, user))
        payload = {
            "network": factories.NetworkFactory.get_url(self.network),
            "target_tenant": factories.TenantFactory.get_url(self.target_tenant),
            "policy_type": models.NetworkRBACPolicy.NetworkShareType.SHARED,
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_member_cannot_create_rbac_policy(self):
        self.client.force_authenticate(self.fixture.member)
        payload = {
            "network": factories.NetworkFactory.get_url(self.network),
            "target_tenant": factories.TenantFactory.get_url(self.target_tenant),
            "policy_type": models.NetworkRBACPolicy.NetworkShareType.SHARED,
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_target_tenant_on_other_service_settings_is_rejected(self):
        # Neutron will not catch this: target_tenant is an opaque string to it,
        # and a real cloud answers 201 for a project id it has never heard of.
        # This check is the only guard, and it stopped running on this endpoint
        # when the action moved to a standalone viewset — the validation was
        # keyed on a context value only the old action supplied.
        foreign_tenant = factories.TenantFactory(backend_id="foreign_backend_id")
        self.assertNotEqual(
            foreign_tenant.service_settings, self.network.tenant.service_settings
        )
        foreign_tenant.project.add_user(self.fixture.staff, ProjectRole.ADMIN)

        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "network": factories.NetworkFactory.get_url(self.network),
            "target_tenant": factories.TenantFactory.get_url(foreign_tenant),
            "policy_type": models.NetworkRBACPolicy.NetworkShareType.SHARED,
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("target_tenant", response.data)
        self.assertFalse(
            models.NetworkRBACPolicy.objects.filter(
                network=self.network, target_tenant=foreign_tenant
            ).exists()
        )

    def test_duplicate_policy_is_rejected(self):
        self.target_tenant.project.add_user(self.fixture.staff, ProjectRole.ADMIN)
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "network": factories.NetworkFactory.get_url(self.network),
            "target_tenant": factories.TenantFactory.get_url(self.target_tenant),
            "policy_type": models.NetworkRBACPolicy.NetworkShareType.SHARED,
        }
        self.assertEqual(
            self.client.post(self.url, payload).status_code,
            status.HTTP_201_CREATED,
        )
        self.assertEqual(
            self.client.post(self.url, payload).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(models.NetworkRBACPolicy.objects.count(), 1)

    def test_backend_duplicate_is_a_conflict_not_a_server_error(self):
        # Reached when two requests clear the uniqueness check before either
        # commits. Neutron answers 409 DuplicateRbacPolicy, but neutronclient
        # has no class for that type, so it arrives as the generic Conflict —
        # which used to escape as an unhandled backend error, i.e. a 500.
        self.mock_neutron_client().create_rbac_policy.side_effect = (
            neutron_exceptions.Conflict(
                message="An RBAC policy already exists with those values."
            )
        )
        self.target_tenant.project.add_user(self.fixture.staff, ProjectRole.ADMIN)
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "network": factories.NetworkFactory.get_url(self.network),
            "target_tenant": factories.TenantFactory.get_url(self.target_tenant),
            "policy_type": models.NetworkRBACPolicy.NetworkShareType.SHARED,
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(models.NetworkRBACPolicy.objects.exists())

    def test_user_with_only_target_tenant_permissions_cannot_create_rbac_policy(self):
        # Symmetric guard for the source-side AND-clause in
        # NetworkRBACPolicyViewSet._check_rbac_policy_permissions: a user who is
        # only an admin on the *recipient* tenant must not be able to share
        # someone else's network into their own tenant.
        target_only_admin = structure_factories.UserFactory()
        self.target_tenant.project.add_user(target_only_admin, ProjectRole.ADMIN)

        self.client.force_authenticate(target_only_admin)
        payload = {
            "network": factories.NetworkFactory.get_url(self.network),
            "target_tenant": factories.TenantFactory.get_url(self.target_tenant),
            "policy_type": models.NetworkRBACPolicy.NetworkShareType.SHARED,
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            models.NetworkRBACPolicy.objects.filter(
                network=self.network, target_tenant=self.target_tenant
            ).exists()
        )


@ddt
class DeleteRbacPolicyTest(test.APITestCase):
    def setUp(self):
        self.neutron_client_patcher = mock.patch(
            "waldur_openstack.backend.get_neutron_client"
        )
        self.mock_neutron_client = self.neutron_client_patcher.start()

        self.keystone_session_patcher = mock.patch(
            "waldur_openstack.backend.get_keystone_session"
        )
        self.mock_keystone_session = self.keystone_session_patcher.start()

        self.fixture = fixtures.OpenStackFixture()
        self.network = self.fixture.network
        self.rbac_policy = factories.NetworkRBACPolicyFactory(network=self.network)
        self.target_tenant = self.rbac_policy.target_tenant
        self.url = f"/api/openstack-network-rbac-policies/{self.rbac_policy.uuid}/"

    def tearDown(self):
        self.neutron_client_patcher.stop()
        super().tearDown()

    @data("staff", "owner", "admin", "manager")
    def test_authorized_user_can_delete_rbac_policy(self, user):
        self.target_tenant.project.add_user(self.fixture.owner, ProjectRole.ADMIN)
        self.target_tenant.project.add_user(self.fixture.admin, ProjectRole.ADMIN)
        self.target_tenant.project.add_user(self.fixture.manager, ProjectRole.MANAGER)

        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.NetworkRBACPolicy.objects.filter(uuid=self.rbac_policy.uuid).exists()
        )

    @data("owner", "admin", "manager")
    def test_user_without_target_tenant_permissions_cannot_delete_rbac_policy(
        self, user
    ):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_member_cannot_delete_rbac_policy(self):
        self.client.force_authenticate(getattr(self.fixture, "member"))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class RbacPolicyVisibilityTest(test.APITestCase):
    """Inbound RBAC policies must be visible to the target tenant's project."""

    def setUp(self):
        # Source side: owns the network being shared.
        self.source = fixtures.OpenStackFixture()
        self.source_network = self.source.network

        # Target side: a separate project on the same service settings,
        # represented by an independent ProjectFixture so that its `owner`,
        # `admin`, and `manager` users belong only to that project.
        self.target = structure_fixtures.ProjectFixture()
        # Cross-link the target project's customer to the same service
        # settings so the FK structure mirrors the production case.
        self.target_tenant = factories.TenantFactory(
            service_settings=self.source.settings,
            project=self.target.project,
            backend_id="target_backend_id",
        )

        self.policy = factories.NetworkRBACPolicyFactory(
            network=self.source_network,
            target_tenant=self.target_tenant,
            policy_type=models.NetworkRBACPolicy.NetworkShareType.SHARED,
        )
        self.list_url = "/api/openstack-network-rbac-policies/"
        self.detail_url = f"{self.list_url}{self.policy.uuid}/"

    def _uuids(self, response):
        return {row["uuid"] for row in response.data}

    def test_target_tenant_owner_sees_inbound_policy(self):
        self.client.force_authenticate(self.target.owner)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(str(self.policy.uuid).replace("-", ""), self._uuids(response))

    def test_target_tenant_admin_sees_inbound_policy(self):
        self.client.force_authenticate(self.target.admin)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(str(self.policy.uuid).replace("-", ""), self._uuids(response))

    def test_source_tenant_owner_still_sees_outbound_policy(self):
        self.client.force_authenticate(self.source.owner)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(str(self.policy.uuid).replace("-", ""), self._uuids(response))

    def test_unrelated_user_does_not_see_policy(self):
        other = structure_fixtures.ProjectFixture()
        self.client.force_authenticate(other.owner)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(str(self.policy.uuid).replace("-", ""), self._uuids(response))

    def test_direction_field_is_outbound_for_source_user(self):
        self.client.force_authenticate(self.source.owner)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["direction"], "outbound")

    def test_direction_field_is_inbound_for_target_user(self):
        self.client.force_authenticate(self.target.owner)
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["direction"], "inbound")

    def test_direction_filter_outbound_excludes_inbound_rows(self):
        # Also create an inbound policy from another source into self.source.project.
        other_source = fixtures.OpenStackFixture()
        inbound_network = other_source.network
        target_for_source = factories.TenantFactory(
            service_settings=other_source.settings,
            project=self.source.project,
            backend_id="inbound_target",
        )
        inbound_policy = factories.NetworkRBACPolicyFactory(
            network=inbound_network,
            target_tenant=target_for_source,
            policy_type=models.NetworkRBACPolicy.NetworkShareType.SHARED,
        )

        self.client.force_authenticate(self.source.owner)
        response = self.client.get(self.list_url + "?direction=outbound")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = self._uuids(response)
        self.assertIn(str(self.policy.uuid).replace("-", ""), uuids)
        self.assertNotIn(str(inbound_policy.uuid).replace("-", ""), uuids)

        response = self.client.get(self.list_url + "?direction=inbound")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = self._uuids(response)
        self.assertIn(str(inbound_policy.uuid).replace("-", ""), uuids)
        self.assertNotIn(str(self.policy.uuid).replace("-", ""), uuids)


class RbacPolicyEventsTest(test.APITestCase):
    """Create / delete must emit the new RBAC event types."""

    def setUp(self):
        self.neutron_client_patcher = mock.patch(
            "waldur_openstack.backend.get_neutron_client"
        )
        self.mock_neutron_client = self.neutron_client_patcher.start()
        self.mock_neutron_client().create_rbac_policy.return_value = {
            "rbac_policy": {"id": "test-rbac-id"}
        }
        self.keystone_session_patcher = mock.patch(
            "waldur_openstack.backend.get_keystone_session"
        )
        self.keystone_session_patcher.start()

        self.fixture = fixtures.OpenStackFixture()
        self.network = self.fixture.network
        self.target_tenant = factories.TenantFactory(
            service_settings=self.fixture.settings, backend_id="other_backend_id"
        )
        self.target_tenant.project.add_user(self.fixture.owner, ProjectRole.ADMIN)

    def tearDown(self):
        self.neutron_client_patcher.stop()
        self.keystone_session_patcher.stop()
        super().tearDown()

    def test_create_emits_event(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "network": factories.NetworkFactory.get_url(self.network),
            "target_tenant": factories.TenantFactory.get_url(self.target_tenant),
            "policy_type": models.NetworkRBACPolicy.NetworkShareType.SHARED,
        }
        with mock.patch("waldur_openstack.views.event_logger.emit") as emit_mock:
            response = self.client.post(
                "/api/openstack-network-rbac-policies/", payload
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        emit_calls = [c for c in emit_mock.call_args_list]
        event_types = [c.kwargs.get("event_type") for c in emit_calls]
        self.assertIn(EventType.OPENSTACK_RBAC_POLICY_CREATED, event_types)

    def test_delete_emits_event(self):
        policy = factories.NetworkRBACPolicyFactory(
            network=self.network, target_tenant=self.target_tenant
        )
        self.client.force_authenticate(self.fixture.staff)
        with mock.patch("waldur_openstack.views.event_logger.emit") as emit_mock:
            response = self.client.delete(
                f"/api/openstack-network-rbac-policies/{policy.uuid}/"
            )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        event_types = [c.kwargs.get("event_type") for c in emit_mock.call_args_list]
        self.assertIn(EventType.OPENSTACK_RBAC_POLICY_DELETED, event_types)
