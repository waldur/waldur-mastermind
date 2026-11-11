"""A role above a PAT's binding counts where an action opts in to binding_on_path.

A binding limits where a token acts and a role says what it may do, so the two
need not sit on the same object. With ``binding_on_path=True``,
``has_permission_on_any_source`` accepts a binding that names the target or an
object on a declared source path, while the role may be on any source. Every
other action keeps the binding on the source holding the role. Each test names
who holds the role, because a staff user would pass every role check and prove
nothing.
"""

from datetime import timedelta
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory
from django.utils import timezone
from rest_framework import status, test
from rest_framework.exceptions import PermissionDenied

from waldur_core.configuration.testing import override_config
from waldur_core.core.enums import ReauthenticationAction
from waldur_core.core.tests.helpers import reauthenticated
from waldur_core.permissions.enums import TYPE_KEY_BY_CT, PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.permissions.tests.test_pat_list_filtering import (
    _auth_header,
    _create_pat,
)
from waldur_core.permissions.utils import (
    build_role_index,
    has_permission_on_any_source,
)
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import AccountScopes
from waldur_mastermind.marketplace.permissions import user_can_terminate_resource
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture
from waldur_mastermind.marketplace_script.tests.fixtures import ScriptFixture
from waldur_mastermind.proposal.tests import factories as proposal_factories
from waldur_mastermind.proposal.tests import fixtures as proposal_fixtures

OFFERING_SOURCES = ["offering", "offering.customer"]
OFFERING_ADMIN_SOURCES = ["*", "customer", "customer.serviceprovider"]
PROJECT_SOURCES = ["*", "customer"]


class BindingSetup:
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.offering = self.resource.offering
        self.provider = self.fixture.service_provider
        self.organization = self.offering.customer
        self.consumer_project = self.resource.project
        self.project = structure_factories.ProjectFactory(customer=self.organization)
        self.owner = self.make_user()
        self.organization.add_user(self.owner, CustomerRole.OWNER)
        for role in (
            CustomerRole.OWNER,
            OfferingRole.MANAGER,
            ProjectRole.MANAGER,
            ServiceProviderRole.MANAGER,
        ):
            for permission in (
                PermissionEnum.CREATE_PROJECT,
                PermissionEnum.DELETE_PROJECT,
                PermissionEnum.LIST_PROJECTS,
                PermissionEnum.SET_RESOURCE_STATE,
                PermissionEnum.UPDATE_OFFERING,
                PermissionEnum.UPDATE_OFFERING_INTEGRATION,
                PermissionEnum.UPDATE_OFFERING_USER,
                PermissionEnum.UPDATE_PROJECT,
            ):
                role.add_permission(permission)

    def make_user(self, **kwargs):
        return structure_factories.UserFactory(
            can_use_personal_access_tokens=True, **kwargs
        )


@override_config(PAT_ENABLED=True)
class SourcePathRuleTest(BindingSetup, test.APITestCase):
    def request(self, user, binding, permission, scopes=None):
        pat = _create_pat(
            user,
            scopes=[permission.value] if scopes is None else scopes,
            bindings=[binding] if binding else [],
        )
        request = RequestFactory().get("/")
        request.user = user
        request.auth = pat
        return request

    def allows(
        self,
        user,
        binding,
        scope,
        sources=None,
        permission=PermissionEnum.SET_RESOURCE_STATE,
        scopes=None,
        **kwargs,
    ):
        kwargs.setdefault("binding_on_path", True)
        return has_permission_on_any_source(
            self.request(user, binding, permission, scopes),
            permission,
            scope,
            OFFERING_SOURCES if sources is None else sources,
            **kwargs,
        )

    def test_binding_on_the_target(self):
        self.assertTrue(
            self.allows(
                self.owner,
                self.project,
                self.project,
                PROJECT_SOURCES,
                PermissionEnum.UPDATE_PROJECT,
            )
        )

    def test_binding_on_a_target_that_is_not_a_source(self):
        self.assertTrue(
            self.allows(
                self.owner,
                self.project,
                self.project,
                ["customer"],
                PermissionEnum.DELETE_PROJECT,
            )
        )

    def test_binding_on_a_source_above_which_the_role_sits(self):
        self.assertTrue(self.allows(self.owner, self.offering, self.resource))

    def test_binding_on_an_object_passed_on_the_way_to_the_source(self):
        self.assertTrue(
            self.allows(
                self.owner,
                self.offering,
                self.fixture.plan,
                ["offering.customer"],
                PermissionEnum.UPDATE_OFFERING,
            )
        )

    def test_binding_on_the_target_with_a_role_on_a_source(self):
        manager = self.make_user()
        self.offering.add_user(manager, OfferingRole.MANAGER)
        self.assertTrue(self.allows(manager, self.resource, self.resource))

    def test_provider_binding_covers_an_offering_on_the_path(self):
        self.assertTrue(self.allows(self.owner, self.provider, self.resource))

    def test_provider_binding_covers_an_offering_target(self):
        self.assertTrue(
            self.allows(
                self.owner,
                self.provider,
                self.offering,
                ["customer"],
                PermissionEnum.UPDATE_OFFERING,
            )
        )

    def test_provider_binding_covers_itself(self):
        self.assertTrue(
            self.allows(
                self.owner,
                self.provider,
                self.provider,
                ["customer"],
                PermissionEnum.UPDATE_OFFERING,
            )
        )

    def test_provider_binding_stays_off_customer_wide_actions(self):
        self.assertFalse(
            self.allows(
                self.owner,
                self.provider,
                self.project,
                PROJECT_SOURCES,
                PermissionEnum.UPDATE_PROJECT,
            )
        )

    def test_consumer_binding_stays_off_provider_sources(self):
        self.consumer_project.add_user(self.owner, ProjectRole.MANAGER)
        self.assertFalse(self.allows(self.owner, self.consumer_project, self.resource))

    def test_consumer_binding_stays_off_provider_sources_of_a_private_offering(self):
        """``get_scope_ancestors`` walks an offering's project, so a walk upward
        from a source would let this binding through."""
        self.offering.project = self.consumer_project
        self.offering.save(update_fields=["project"])
        self.consumer_project.add_user(self.owner, ProjectRole.MANAGER)
        self.assertFalse(self.allows(self.owner, self.consumer_project, self.resource))

    def test_binding_on_a_sibling_is_refused(self):
        sibling_offering = marketplace_factories.OfferingFactory(
            customer=self.organization
        )
        sibling_project = structure_factories.ProjectFactory(customer=self.organization)
        self.assertFalse(self.allows(self.owner, sibling_offering, self.resource))
        self.assertFalse(
            self.allows(
                self.owner,
                sibling_project,
                self.project,
                PROJECT_SOURCES,
                PermissionEnum.UPDATE_PROJECT,
            )
        )

    def test_binding_on_the_target_grants_no_role(self):
        self.assertFalse(self.allows(self.make_user(), self.offering, self.resource))

    def test_token_without_the_permission_is_refused(self):
        self.assertFalse(
            self.allows(
                self.owner,
                self.offering,
                self.resource,
                scopes=[PermissionEnum.LIST_PROJECTS.value],
            )
        )

    def test_by_default_the_binding_stays_on_the_source_holding_the_role(self):
        self.assertFalse(
            self.allows(self.owner, self.offering, self.resource, binding_on_path=False)
        )
        self.assertTrue(
            self.allows(
                self.owner, self.organization, self.resource, binding_on_path=False
            )
        )

    def test_terminate_does_not_opt_in(self):
        """Its sources name both sides, so an offering binding would let a
        provider role reach the consumer's resource."""
        CustomerRole.OWNER.add_permission(PermissionEnum.TERMINATE_RESOURCE)
        for binding, allowed in ((self.offering, False), (self.organization, True)):
            with self.subTest(binding=type(binding).__name__):
                request = self.request(
                    self.owner, binding, PermissionEnum.TERMINATE_RESOURCE
                )
                if allowed:
                    user_can_terminate_resource(request, None, self.resource)
                else:
                    with self.assertRaises(PermissionDenied):
                        user_can_terminate_resource(request, None, self.resource)

    def test_organization_binding_is_unchanged(self):
        self.assertTrue(self.allows(self.owner, self.organization, self.resource))

    def test_provider_binding_through_a_source_is_kept(self):
        """No offering on this path: the provider is matched as a source."""
        manager = self.make_user()
        self.provider.add_user(manager, ServiceProviderRole.MANAGER)
        group = marketplace_factories.OfferingGroupFactory(customer=self.organization)
        self.assertTrue(
            self.allows(
                manager,
                self.provider,
                group,
                OFFERING_ADMIN_SOURCES,
                PermissionEnum.UPDATE_OFFERING,
            )
        )
        self.assertTrue(
            self.allows(
                manager,
                self.provider,
                self.organization,
                ["*", "serviceprovider"],
                PermissionEnum.UPDATE_OFFERING,
            )
        )

    def test_staff_token_is_narrowed_by_the_same_rule(self):
        staff = self.make_user(is_staff=True)
        self.assertTrue(self.allows(staff, self.offering, self.resource))
        self.assertFalse(
            self.allows(staff, marketplace_factories.OfferingFactory(), self.resource)
        )

    def test_a_user_in_place_of_a_request_has_no_token(self):
        permission = PermissionEnum.SET_RESOURCE_STATE
        self.assertTrue(
            has_permission_on_any_source(
                self.owner, permission, self.resource, OFFERING_SOURCES
            )
        )
        self.assertFalse(
            has_permission_on_any_source(
                self.make_user(), permission, self.resource, OFFERING_SOURCES
            )
        )

    def test_request_without_a_token_needs_only_the_role(self):
        for user, expected in ((self.owner, True), (self.make_user(), False)):
            request = RequestFactory().get("/")
            request.user = user
            request.auth = None
            self.assertEqual(
                has_permission_on_any_source(
                    request,
                    PermissionEnum.SET_RESOURCE_STATE,
                    self.resource,
                    OFFERING_SOURCES,
                ),
                expected,
            )

    def test_unbound_token_needs_only_the_role(self):
        self.assertTrue(self.allows(self.owner, None, self.resource))
        self.assertFalse(self.allows(self.make_user(), None, self.resource))

    def test_paths_that_all_fail_still_raise(self):
        with self.assertRaises(AttributeError):
            self.allows(self.owner, self.offering, self.resource, ["no.such.path"])

    def test_a_failing_path_does_not_stop_the_others(self):
        customer = structure_factories.CustomerFactory()
        offering = marketplace_factories.OfferingFactory(customer=customer)
        customer.add_user(self.owner, CustomerRole.OWNER)
        self.assertFalse(hasattr(customer, "serviceprovider"))
        self.assertTrue(
            self.allows(
                self.owner,
                offering,
                offering,
                OFFERING_ADMIN_SOURCES,
                PermissionEnum.UPDATE_OFFERING,
            )
        )

    def test_a_source_that_is_null_is_skipped(self):
        offering = marketplace_factories.OfferingFactory(customer=None)
        self.assertFalse(
            self.allows(
                self.owner,
                offering,
                offering,
                PROJECT_SOURCES,
                PermissionEnum.UPDATE_OFFERING,
            )
        )

    def test_role_index_answers_the_role_half(self):
        permission = PermissionEnum.SET_RESOURCE_STATE
        index = build_role_index(self.owner, [(permission, self.organization)])
        self.assertTrue(
            self.allows(self.owner, self.offering, self.resource, role_index=index)
        )
        customer_ct = ContentType.objects.get_for_model(structure_models.Customer)
        index[(permission, customer_ct.id, self.organization.id)] = False
        self.assertFalse(
            self.allows(self.owner, self.offering, self.resource, role_index=index)
        )


@override_config(PAT_ENABLED=True, ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
class SourcePathApiTest(BindingSetup, test.APITestCase):
    """Tokens are issued through the API, so its creation guard runs too."""

    def authenticate(self, user, binding, permission):
        content_type = ContentType.objects.get_for_model(type(binding))
        type_key = TYPE_KEY_BY_CT[(content_type.app_label, content_type.model)]
        self.client.force_authenticate(user=user)
        response = self.client.post(
            "/api/personal-access-tokens/",
            {
                "name": "bound",
                "scopes": [permission.value],
                "expires_at": (timezone.now() + timedelta(days=30)).isoformat(),
                "allowed_scopes": [{"type": type_key, "uuid": binding.uuid.hex}],
            },
            format="json",
            **reauthenticated(
                user, ReauthenticationAction.CREATE_PERSONAL_ACCESS_TOKEN
            ),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.client.force_authenticate(user=None)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['token']}")

    def refresh_last_sync(self):
        return self.client.post(
            marketplace_factories.ResourceFactory.get_provider_resource_url(
                self.resource, action="refresh_last_sync"
            )
        )

    def update_overview(self):
        return self.client.post(
            marketplace_factories.OfferingFactory.get_url(
                self.offering, "update_overview"
            ),
            {"name": "renamed"},
        )

    def patch_project(self, project):
        return self.client.patch(
            structure_factories.ProjectFactory.get_url(project),
            {"description": "changed"},
            format="json",
        )

    def test_owner_writes_back_with_an_offering_binding(self):
        self.authenticate(self.owner, self.offering, PermissionEnum.SET_RESOURCE_STATE)
        self.assertEqual(self.refresh_last_sync().status_code, status.HTTP_200_OK)

    def test_owner_writes_back_with_a_provider_binding(self):
        self.authenticate(self.owner, self.provider, PermissionEnum.SET_RESOURCE_STATE)
        self.assertEqual(self.refresh_last_sync().status_code, status.HTTP_200_OK)

    def test_owner_binding_to_the_organization_still_misses_other_consumers(self):
        """The Resource filter rule keeps it off resources ordered by others."""
        self.authenticate(
            self.owner, self.organization, PermissionEnum.SET_RESOURCE_STATE
        )
        self.assertEqual(
            self.refresh_last_sync().status_code, status.HTTP_404_NOT_FOUND
        )

    def test_offering_manager_writes_back_with_a_resource_binding(self):
        manager = self.make_user()
        self.offering.add_user(manager, OfferingRole.MANAGER)
        self.authenticate(manager, self.resource, PermissionEnum.SET_RESOURCE_STATE)
        self.assertEqual(self.refresh_last_sync().status_code, status.HTTP_200_OK)

    def test_consumer_binding_cannot_write_back(self):
        self.consumer_project.add_user(self.owner, ProjectRole.MANAGER)
        self.authenticate(
            self.owner, self.consumer_project, PermissionEnum.SET_RESOURCE_STATE
        )
        self.assertEqual(
            self.refresh_last_sync().status_code, status.HTTP_403_FORBIDDEN
        )

    def test_owner_updates_an_offering_with_an_offering_binding(self):
        self.authenticate(self.owner, self.offering, PermissionEnum.UPDATE_OFFERING)
        self.assertEqual(self.update_overview().status_code, status.HTTP_200_OK)

    def test_owner_updates_an_offering_with_a_provider_binding(self):
        self.authenticate(self.owner, self.provider, PermissionEnum.UPDATE_OFFERING)
        self.assertEqual(self.update_overview().status_code, status.HTTP_200_OK)

    def test_offering_update_tolerates_a_customer_without_a_provider(self):
        customer = structure_factories.CustomerFactory()
        customer.add_user(self.owner, CustomerRole.OWNER)
        self.offering = marketplace_factories.OfferingFactory(customer=customer)
        self.authenticate(self.owner, self.offering, PermissionEnum.UPDATE_OFFERING)
        self.assertEqual(self.update_overview().status_code, status.HTTP_200_OK)

    def test_sibling_offering_binding_does_not_find_the_offering(self):
        """The token's list filter answers first; the rule is covered by
        ``SourcePathRuleTest.test_binding_on_a_sibling_is_refused``."""
        sibling = marketplace_factories.OfferingFactory(customer=self.organization)
        self.authenticate(self.owner, sibling, PermissionEnum.UPDATE_OFFERING)
        self.assertEqual(self.update_overview().status_code, status.HTTP_404_NOT_FOUND)

    def test_owner_updates_a_project_with_a_project_binding(self):
        self.authenticate(self.owner, self.project, PermissionEnum.UPDATE_PROJECT)
        self.assertEqual(
            self.patch_project(self.project).status_code, status.HTTP_200_OK
        )

    def test_sibling_project_binding_does_not_find_the_project(self):
        """The token's list filter answers first, as for the offering above."""
        sibling = structure_factories.ProjectFactory(customer=self.organization)
        self.authenticate(self.owner, sibling, PermissionEnum.UPDATE_PROJECT)
        self.assertEqual(
            self.patch_project(self.project).status_code, status.HTTP_404_NOT_FOUND
        )


class NotOptedInMixin:
    def post(self, url, data):
        return self.client.post(url, data or {}, format="json")

    def assert_binding_stays_on_source(self, user, binding, permission, url, data=None):
        """Refused with the binding, and past the gate without it, so the
        refusal cannot have some other cause."""
        pat = _create_pat(user, scopes=[permission.value], bindings=[binding])
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        self.assertEqual(self.post(url, data).status_code, status.HTTP_403_FORBIDDEN)

        pat = _create_pat(user, scopes=[permission.value], bindings=[])
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        self.assertNotEqual(self.post(url, data).status_code, status.HTTP_403_FORBIDDEN)


@override_config(PAT_ENABLED=True)
class NotOptedInActionsTest(NotOptedInMixin, BindingSetup, test.APITestCase):
    """Actions reaching beyond their target do not opt in, so the binding has to
    cover the source holding the role."""

    def assert_unchanged_on_offering_user(self, action):
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save(update_fields=["plugin_options"])
        offering_user = marketplace_factories.OfferingUserFactory(
            offering=self.offering
        )
        self.assert_binding_stays_on_source(
            self.owner,
            self.offering,
            PermissionEnum.UPDATE_OFFERING_USER,
            f"/api/marketplace-offering-users/{offering_user.uuid.hex}/{action}/",
        )

    def test_set_posix_attributes(self):
        self.assert_unchanged_on_offering_user("set_posix_attributes")

    def test_set_deleted(self):
        self.assert_unchanged_on_offering_user("set_deleted")

    def test_request_deletion(self):
        self.assert_unchanged_on_offering_user("request_deletion")

    def test_check_unique_backend_id(self):
        self.assert_binding_stays_on_source(
            self.owner,
            self.offering,
            PermissionEnum.UPDATE_OFFERING,
            marketplace_factories.OfferingFactory.get_url(
                self.offering, "check_unique_backend_id"
            ),
            {"backend_id": "backend"},
        )

    def test_update_integration(self):
        self.assert_binding_stays_on_source(
            self.owner,
            self.offering,
            PermissionEnum.UPDATE_OFFERING_INTEGRATION,
            marketplace_factories.OfferingFactory.get_url(
                self.offering, "update_integration"
            ),
        )

    def test_recover(self):
        self.project.delete()
        self.assert_binding_stays_on_source(
            self.owner,
            self.project,
            PermissionEnum.CREATE_PROJECT,
            structure_factories.ProjectFactory.get_url(self.project, action="recover"),
        )


@override_config(PAT_ENABLED=True)
class CallDuplicateNotOptedInTest(NotOptedInMixin, test.APITestCase):
    def test_duplicate(self):
        fixture = proposal_fixtures.ProposalFixture()
        fixture.call_organizer_role.add_permission(PermissionEnum.CREATE_CALL)
        user = fixture.call_organizer_user
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])
        self.assert_binding_stays_on_source(
            user,
            fixture.call,
            PermissionEnum.CREATE_CALL,
            proposal_factories.CallFactory.get_protected_url(fixture.call, "duplicate"),
            {"name": "Copy of call"},
        )


@override_config(PAT_ENABLED=True)
@mock.patch("waldur_mastermind.marketplace_script.utils.execute_script")
class DryRunNotOptedInTest(NotOptedInMixin, test.APITestCase):
    def setUp(self):
        self.fixture = ScriptFixture()
        self.offering = self.fixture.offering
        self.offering.options.update({"option1": []})
        self.offering.customer = self.fixture.offering_customer
        self.offering.project = self.fixture.offering_project
        self.offering.save()
        self.owner = self.fixture.offering_owner
        self.owner.can_use_personal_access_tokens = True
        self.owner.save(update_fields=["can_use_personal_access_tokens"])
        CustomerRole.OWNER.add_permission(PermissionEnum.DRY_RUN_OFFERING_SCRIPT)
        self.data = {
            "plan": marketplace_factories.PlanFactory.get_url(self.fixture.plan),
            "type": "Create",
            "attributes": {"option1": "value1"},
        }

    def assert_creates_no_project(self, url):
        projects = structure_models.Project.objects.count()
        pat = _create_pat(
            self.owner,
            scopes=[PermissionEnum.DRY_RUN_OFFERING_SCRIPT.value],
            bindings=[self.offering],
        )
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.post(url, self.data, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(structure_models.Project.objects.count(), projects)

    def test_run(self, execute_script):
        execute_script.return_value = "output"
        self.assert_creates_no_project(self.fixture.get_dry_run_url(self.offering))
        self.assert_binding_stays_on_source(
            self.owner,
            self.offering,
            PermissionEnum.DRY_RUN_OFFERING_SCRIPT,
            self.fixture.get_dry_run_url(self.offering),
            self.data,
        )

    def test_async_run(self, execute_script):
        self.assert_creates_no_project(
            self.fixture.get_async_dry_run_url(self.offering)
        )
        self.assert_binding_stays_on_source(
            self.owner,
            self.offering,
            PermissionEnum.DRY_RUN_OFFERING_SCRIPT,
            self.fixture.get_async_dry_run_url(self.offering),
            self.data,
        )


@override_config(PAT_ENABLED=True)
class NotOptedInSharedScopeTest(NotOptedInMixin, test.APITestCase):
    """An offering's scope may be shared by other offerings, so neither its
    integration settings nor its sync opt in."""

    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        marketplace_factories.ServiceProviderFactory(customer=self.customer)
        self.settings = structure_factories.ServiceSettingsFactory(
            customer=self.customer,
            backend_url="https://backend.example.com",
            username="admin",
            password="shared-secret",
        )
        self.offering = marketplace_factories.OfferingFactory(
            customer=self.customer, shared=True, scope=self.settings
        )
        self.owner = structure_factories.UserFactory(
            can_use_personal_access_tokens=True
        )
        self.customer.add_user(self.owner, CustomerRole.OWNER)

    def use_token(self, permission, *bindings):
        pat = _create_pat(self.owner, scopes=[permission.value], bindings=bindings)
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))

    def test_integration_settings_are_read_as_they_are_written(self):
        permission = PermissionEnum.UPDATE_OFFERING_INTEGRATION
        CustomerRole.OWNER.add_permission(permission)
        url = marketplace_factories.OfferingFactory.get_url(self.offering)
        update_url = marketplace_factories.OfferingFactory.get_url(
            self.offering, "update_integration"
        )
        for bindings, allowed in (((), True), ((self.offering,), False)):
            with self.subTest(bound=bool(bindings)):
                self.use_token(permission, *bindings)
                response = self.client.get(url)
                write = self.post(update_url, {"backend_id": "x"})

                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.data["can_update_integration"], allowed)
                self.assertEqual(
                    write.status_code != status.HTTP_403_FORBIDDEN, allowed
                )
                attributes = response.data.get("service_attributes") or {}
                self.assertEqual(attributes.get("password") == "shared-secret", allowed)
                self.assertEqual("secret_options" in response.data, allowed)

    def test_sync(self):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)
        marketplace_factories.OfferingFactory(
            customer=self.customer, shared=True, scope=self.settings
        )
        self.assert_binding_stays_on_source(
            self.owner,
            self.offering,
            PermissionEnum.UPDATE_OFFERING_COMPONENTS,
            marketplace_factories.OfferingFactory.get_url(self.offering, "sync"),
        )


@override_config(PAT_ENABLED=True)
class NotOptedInProviderAccountTest(test.APITestCase):
    """Creating an offering user writes the provider-wide account."""

    def setUp(self):
        fixture = MarketplaceFixture()
        self.provider = fixture.service_provider
        self.provider.account_options["account_scope"] = AccountScopes.PROVIDER
        self.provider.save()
        self.offering = fixture.offering
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": True
        }
        self.offering.save()
        self.owner = structure_factories.UserFactory(
            can_use_personal_access_tokens=True
        )
        self.offering.customer.add_user(self.owner, CustomerRole.OWNER)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING_USER)
        self.person = structure_factories.UserFactory()

    def create(self, *bindings):
        pat = _create_pat(
            self.owner,
            scopes=[PermissionEnum.CREATE_OFFERING_USER.value],
            bindings=bindings,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        return self.client.post(
            marketplace_factories.OfferingUserFactory.get_list_url(),
            {
                "offering": marketplace_factories.OfferingFactory.get_url(
                    self.offering
                ),
                "user": structure_factories.UserFactory.get_url(self.person),
            },
        )

    def provider_account_exists(self):
        return marketplace_models.ServiceProviderAccount.objects.filter(
            service_provider=self.provider, user=self.person
        ).exists()

    def test_offering_binding_does_not_create_it(self):
        self.assertEqual(
            self.create(self.offering).status_code, status.HTTP_403_FORBIDDEN
        )
        self.assertFalse(self.provider_account_exists())

    def test_unbound_token_creates_it(self):
        response = self.create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(self.provider_account_exists())
