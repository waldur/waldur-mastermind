"""Who may reveal a resource API key, and with which credential."""

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test
from rest_framework.authtoken.models import Token
from rest_framework.reverse import reverse

from waldur_core.configuration.testing import override_config
from waldur_core.core import encryption
from waldur_core.core.tests.helpers import create_pat
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, GlobalRole, ProjectRole
from waldur_core.permissions.models import Role
from waldur_core.permissions.utils import grant_global_role
from waldur_core.structure.models import Project
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture
from waldur_mastermind.marketplace.tests.test_resource_api_key_provider_list import (
    manage_keys,
)

States = models.ResourceApiKey.States
REVEAL = PermissionEnum.REVEAL_RESOURCE_API_KEY
IMPERSONATED_USER_HEADER = settings.WALDUR_CORE["REQUEST_HEADER_IMPERSONATED_USER_UUID"]


def list_url():
    return reverse("marketplace-resource-api-key-list")


def reveal_url(api_key):
    return reverse(
        "marketplace-resource-api-key-reveal", kwargs={"uuid": api_key.uuid.hex}
    )


def binding(entity):
    ct = ContentType.objects.get_for_model(type(entity))
    return {"content_type_id": ct.id, "object_id": entity.id}


class RevealTestBase(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        # As permissions.yaml grants it. The fixture's system roles start
        # without permissions.
        self.customer_holders = (CustomerRole.OWNER,)
        self.project_holders = (
            ProjectRole.ADMIN,
            ProjectRole.MANAGER,
            ProjectRole.MEMBER,
            ProjectRole.PRINCIPAL_INVESTIGATOR,
            ProjectRole.CO_INVESTIGATOR,
        )
        for role in self.customer_holders + self.project_holders:
            role.add_permission(REVEAL)
        self.key = self.make_key("cid-1")

    def make_key(self, client_id, **kwargs):
        return models.ResourceApiKey.objects.create(
            resource=self.resource,
            client_id=client_id,
            key_ciphertext=encryption.encrypt_value(f"sk-{client_id}"),
            state=States.OK,
            **kwargs,
        )

    def reveal(self, user, key=None):
        self.client.force_authenticate(user)
        return self.client.get(reveal_url(key or self.key))

    def project_user(self, role):
        user = structure_factories.UserFactory()
        self.project.add_user(user, role)
        return user

    def customer_user(self, role):
        user = structure_factories.UserFactory()
        self.customer.add_user(user, role)
        return user


class RevealPermissionTest(RevealTestBase):
    def test_roles_holding_the_permission_reveal_an_unassigned_key(self):
        users = [
            (role.name, self.customer_user(role)) for role in self.customer_holders
        ]
        users += [(role.name, self.project_user(role)) for role in self.project_holders]
        for name, user in users:
            with self.subTest(name):
                response = self.reveal(user)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.data["api_key"], "sk-cid-1")

    def test_roles_without_the_permission_cannot_reveal_an_unassigned_key(self):
        empty_role = Role.objects.create(
            name="PROJECT.acme.EMPTY",
            content_type=ContentType.objects.get_for_model(Project),
        )
        users = [
            ("CUSTOMER.SUPPORT", self.customer_user(CustomerRole.SUPPORT)),
            ("CUSTOMER.READER", self.customer_user(CustomerRole.READER)),
            ("PROJECT.COLLABORATOR", self.project_user(ProjectRole.COLLABORATOR)),
            ("a role with no permissions", self.project_user(empty_role)),
        ]
        for name, user in users:
            with self.subTest(name):
                # They still see the key; only its value is withheld.
                self.client.force_authenticate(user)
                listed = self.client.get(list_url())
                self.assertEqual(
                    [row["uuid"] for row in listed.data], [self.key.uuid.hex]
                )
                response = self.reveal(user)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_site_wide_grant_does_not_count(self):
        site_role = GlobalRole.get("KEY_READER")
        site_role.add_permission(REVEAL)
        user = self.project_user(ProjectRole.COLLABORATOR)
        grant_global_role(user, site_role)
        response = self.reveal(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_holding_a_role_without_the_permission_cannot_reveal(self):
        self.project.add_user(self.fixture.staff, ProjectRole.COLLABORATOR)
        response = self.reveal(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_role_on_another_organization_does_not_count(self):
        other = MarketplaceFixture()
        response = self.reveal(other.owner)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class RevealAssignedKeyTest(RevealTestBase):
    def setUp(self):
        super().setUp()
        self.collaborator = self.project_user(ProjectRole.COLLABORATOR)
        self.key.user = self.collaborator
        self.key.save()

    def test_the_assignee_reveals_it_without_the_permission(self):
        response = self.reveal(self.collaborator)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["api_key"], "sk-cid-1")

    def test_holders_of_the_permission_cannot_reveal_it(self):
        for user in (self.fixture.owner, self.fixture.admin, self.fixture.manager):
            with self.subTest(user=user):
                response = self.reveal(user)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@override_config(PAT_ENABLED=True)
class RevealWithAccessTokenTest(RevealTestBase):
    def reveal_with_pat(self, user, key=None, **pat_kwargs):
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])
        _, token = create_pat(user, **pat_kwargs)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return self.client.get(reveal_url(key or self.key))

    def test_a_personal_access_token_never_reveals_a_key(self):
        owner = self.fixture.owner
        cases = {
            "no scopes": {"scopes": []},
            "unrelated scope": {"scopes": [PermissionEnum.LIST_ORDERS.value]},
            "the reveal permission": {"scopes": [REVEAL.value]},
            "bound to this organization": {
                "scopes": [REVEAL.value],
                "allowed_scopes": [binding(self.customer)],
            },
        }
        for name, pat_kwargs in cases.items():
            with self.subTest(name):
                response = self.reveal_with_pat(owner, **pat_kwargs)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_token_bound_elsewhere_does_not_find_the_key(self):
        response = self.reveal_with_pat(
            self.fixture.owner,
            scopes=[REVEAL.value],
            allowed_scopes=[binding(structure_factories.CustomerFactory())],
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_not_even_the_assignees_own_token(self):
        assigned = self.make_key("cid-2", user=self.fixture.admin)
        response = self.reveal_with_pat(
            self.fixture.admin, assigned, scopes=[REVEAL.value]
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_an_oidc_access_token_reveals_what_its_user_may(self):
        # With OIDC_ACCESS_TOKEN_ENABLED it is what HomePort signs in with.
        assigned = self.make_key("cid-2", user=self.fixture.admin)
        cases = (
            (self.fixture.owner, self.key, status.HTTP_200_OK),
            (self.fixture.admin, assigned, status.HTTP_200_OK),
            (self.fixture.owner, assigned, status.HTTP_403_FORBIDDEN),
            (
                self.project_user(ProjectRole.COLLABORATOR),
                self.key,
                status.HTTP_403_FORBIDDEN,
            ),
        )
        for user, key, expected in cases:
            with self.subTest(user=user, key=key.client_id):
                self.client.force_authenticate(user, token={"sub": "homeport"})
                response = self.client.get(reveal_url(key))
                self.assertEqual(response.status_code, expected)

    def test_the_users_own_token_reveals_it(self):
        token = Token.objects.get_or_create(user=self.fixture.owner)[0]
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        response = self.client.get(reveal_url(self.key))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_staff_impersonating_a_holder_reveal_an_unassigned_key(self):
        token = Token.objects.get_or_create(user=self.fixture.staff)[0]
        self.client.credentials(
            **{
                "HTTP_AUTHORIZATION": f"Token {token.key}",
                IMPERSONATED_USER_HEADER: self.fixture.owner.uuid.hex,
            }
        )
        response = self.client.get(reveal_url(self.key))
        self.assertEqual(response.status_code, status.HTTP_200_OK)


@override_config(PAT_ENABLED=True)
class ApiKeyListWithBoundTokenTest(RevealTestBase):
    def list_with_pat(self, user, *bindings, **params):
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])
        _, token = create_pat(
            user,
            scopes=[PermissionEnum.LIST_ORDERS.value],
            allowed_scopes=[binding(entity) for entity in bindings],
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.get(list_url(), params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [row["uuid"] for row in response.data]

    def test_a_token_bound_elsewhere_lists_no_keys(self):
        other = structure_factories.CustomerFactory()
        self.assertEqual(self.list_with_pat(self.fixture.owner, other), [])

    def test_a_token_bound_to_what_holds_the_resource_lists_its_keys(self):
        for entity in (self.customer, self.project, self.resource):
            with self.subTest(type(entity).__name__):
                self.assertEqual(
                    self.list_with_pat(self.fixture.owner, entity),
                    [self.key.uuid.hex],
                )

    def test_a_token_bound_to_one_resource_skips_its_neighbours(self):
        neighbour = factories.ResourceFactory(
            offering=self.fixture.offering, project=self.project
        )
        models.ResourceApiKey.objects.create(
            resource=neighbour, client_id="cid-x", state=States.OK
        )
        self.assertEqual(
            self.list_with_pat(self.fixture.owner, self.resource),
            [self.key.uuid.hex],
        )

    def test_a_provider_token_bound_to_the_offering_or_provider_lists_its_keys(
        self,
    ):
        manage_keys(self.fixture.offering)
        provider = {"provider_uuid": self.fixture.offering_customer.uuid.hex}
        for entity in (self.fixture.offering, self.fixture.service_provider):
            with self.subTest(type(entity).__name__):
                self.assertEqual(
                    self.list_with_pat(self.fixture.offering_owner, entity, **provider),
                    [self.key.uuid.hex],
                )

    def test_a_binding_on_the_offerings_organization_does_not_reach_consumer_keys(
        self,
    ):
        # As for resources: the organization owning the offering is not the
        # consumer's, and a binding on it lists none of their keys.
        manage_keys(self.fixture.offering)
        self.assertEqual(
            self.list_with_pat(
                self.fixture.offering_owner,
                self.fixture.offering_customer,
                provider_uuid=self.fixture.offering_customer.uuid.hex,
            ),
            [],
        )

    def test_an_unbound_token_lists_what_its_user_sees(self):
        self.assertEqual(self.list_with_pat(self.fixture.owner), [self.key.uuid.hex])
