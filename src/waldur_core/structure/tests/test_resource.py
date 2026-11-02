from ddt import data, ddt
from django.test import SimpleTestCase
from django.urls import get_resolver
from rest_framework import status, test

from waldur_core.core.tests.test_actions_permission import iter_routes
from waldur_core.logging.tests.factories import EventFactory
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.tests import factories, fixtures
from waldur_core.structure.tests import models as test_models
from waldur_core.structure.tests.views import TestNewInstanceViewSet
from waldur_core.structure.views import CreateRequiresProjectAdminMixin
from waldur_mastermind.common import utils as common_utils
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class ResourceRemovalTest(test.APITestCase):
    def setUp(self):
        self.user = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.user)

    def test_when_virtual_machine_is_deleted_descendant_resources_unlinked(self):
        # Arrange
        vm = factories.TestNewInstanceFactory()
        settings = factories.ServiceSettingsFactory(scope=vm)
        child_vm = factories.TestNewInstanceFactory(service_settings=settings)
        other_vm = factories.TestNewInstanceFactory()

        # Act
        vm.delete()

        # Assert
        self.assertFalse(
            test_models.TestNewInstance.objects.filter(id=child_vm.id).exists()
        )
        self.assertFalse(ServiceSettings.objects.filter(id=settings.id).exists())
        self.assertTrue(
            test_models.TestNewInstance.objects.filter(id=other_vm.id).exists()
        )


class ResourceCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.url = factories.TestNewInstanceFactory.get_list_url()

    def test_shared_key_is_valid_for_virtual_machine_serializer(self):
        shared_key = factories.SshPublicKeyFactory(is_shared=True)
        key_url = factories.SshPublicKeyFactory.get_url(shared_key)

        payload = {
            "ssh_public_key": key_url,
            "service_settings": factories.ServiceSettingsFactory.get_url(
                self.fixture.service_settings
            ),
            "project": factories.ProjectFactory.get_url(self.fixture.project),
            "name": "resource name",
        }

        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


@ddt
class ResourceCreatePermissionTest(test.APITestCase):
    """Creating a resource takes what changing it takes.

    Seeing the project is not enough: a project member, customer support and
    global support all see it and may not create resources in it.
    """

    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.url = factories.TestNewInstanceFactory.get_list_url()
        self.payload = {
            "service_settings": factories.ServiceSettingsFactory.get_url(
                self.fixture.service_settings
            ),
            "project": factories.ProjectFactory.get_url(self.fixture.project),
            "name": "resource name",
        }

    @data("staff", "owner", "admin", "manager")
    def test_who_manages_the_project_can_create(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @data("member", "customer_support", "global_support")
    def test_who_only_sees_the_project_cannot_create(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)
        self.assertFalse(test_models.TestNewInstance.objects.exists())

    def test_who_cannot_see_the_project_cannot_create(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.post(self.url, self.payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(test_models.TestNewInstance.objects.exists())

    def test_request_waldur_dispatches_to_itself_is_not_checked_again(self):
        # A marketplace order is carried out through the plugin's viewset as
        # the user who placed it, who may be a project member.
        view = TestNewInstanceViewSet.as_view({"post": "create"})
        response = common_utils.create_request(view, self.fixture.member, self.payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class CreateScopeCoverageTest(SimpleTestCase):
    """Every routed create that inherits the check either applies it or is known not to.

    The check reads the scope from a writable serializer field. A view whose
    field is missing, read-only or misspelled in ``create_scope_field`` gets
    no check at all, so each such view must be listed here on purpose: its
    serializer derives the project from another object and checks it there.
    """

    SCOPE_CHECKED_BY_SERIALIZER = {
        # The port's tenant decides the project; the serializer checks it.
        "waldur_openstack.views.PortViewSet",
        # Created through the tenant's create_server_group action instead.
        "waldur_openstack.views.ServerGroupViewSet",
        # Node creation is staff-only (is_staff on the view).
        "waldur_rancher.views.NodeViewSet",
    }

    def test_every_create_checks_its_scope_or_is_listed(self):
        unchecked = set()
        for callback, _ in iter_routes(get_resolver().url_patterns):
            cls = getattr(callback, "cls", None)
            actions = getattr(callback, "actions", None) or {}
            if actions.get("post") != "create":
                continue
            if not (cls and issubclass(cls, CreateRequiresProjectAdminMixin)):
                continue
            if "create" in getattr(cls, "disabled_actions", []):
                continue
            view = cls()
            view.action = "create"
            view.request = None
            view.format_kwarg = None
            serializer = view.get_serializer_class()(context={"view": view})
            field = serializer.fields.get(cls.create_scope_field)
            if field is None or field.read_only:
                unchecked.add(f"{cls.__module__}.{cls.__name__}")

        self.assertEqual(unchecked, self.SCOPE_CHECKED_BY_SERIALIZER)


@ddt
class ResourceServiceSettingsCustomerTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.other_customer = factories.CustomerFactory()
        self.other_customer.add_user(self.fixture.owner, CustomerRole.OWNER)
        self.url = factories.TestNewInstanceFactory.get_list_url()

    def create_resource(self, user, service_settings):
        self.client.force_authenticate(user=user)
        return self.client.post(
            self.url,
            {
                "service_settings": factories.ServiceSettingsFactory.get_url(
                    service_settings
                ),
                "project": factories.ProjectFactory.get_url(self.fixture.project),
                "name": "resource name",
            },
        )

    @data("staff", "owner")
    def test_private_service_settings_of_other_customer_are_rejected(self, user):
        service_settings = factories.ServiceSettingsFactory(
            customer=self.other_customer, shared=False
        )
        response = self.create_resource(getattr(self.fixture, user), service_settings)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            test_models.TestNewInstance.objects.filter(
                service_settings=service_settings
            ).exists()
        )

    @data("staff", "owner")
    def test_shared_service_settings_of_other_customer_are_accepted(self, user):
        service_settings = factories.ServiceSettingsFactory(
            customer=self.other_customer, shared=True
        )
        response = self.create_resource(getattr(self.fixture, user), service_settings)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("staff", "owner")
    def test_private_service_settings_of_shared_offering_are_accepted(self, user):
        service_settings = factories.ServiceSettingsFactory(
            customer=self.other_customer, shared=False
        )
        marketplace_factories.OfferingFactory(
            customer=self.other_customer, scope=service_settings, shared=True
        )
        response = self.create_resource(getattr(self.fixture, user), service_settings)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("staff", "owner")
    def test_private_service_settings_of_private_offering_are_rejected(self, user):
        service_settings = factories.ServiceSettingsFactory(
            customer=self.other_customer, shared=False
        )
        marketplace_factories.OfferingFactory(
            customer=self.other_customer, scope=service_settings, shared=False
        )
        response = self.create_resource(getattr(self.fixture, user), service_settings)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_private_service_settings_without_customer_are_accepted(self):
        service_settings = factories.ServiceSettingsFactory(customer=None, shared=False)
        response = self.create_resource(self.fixture.staff, service_settings)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("staff", "owner")
    def test_private_service_settings_of_project_customer_are_accepted(self, user):
        response = self.create_resource(
            getattr(self.fixture, user), self.fixture.service_settings
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class ResourceEventsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.client.force_authenticate(user=self.fixture.staff)
        self.url = factories.TestNewInstanceFactory.get_url(self.fixture.resource)

    def test_filter_events_for_resource_by_scope(self):
        response = self.client.get(EventFactory.get_list_url(), {"scope": self.url})
        self.assertEqual(len(response.data), 1)
