import unittest
from unittest.mock import patch

import ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.quotas.fields import TotalQuotaField
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests import models as test_models
from waldur_core.structure.tests import serializers as structure_test_serializers
from waldur_core.structure.tests import views as structure_test_views
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.tests import factories, utils


class CartItemListTest(test.APITransactionTestCase):
    def setUp(self):
        self.cart_item = factories.CartItemFactory()

    def test_cart_item_renders_attributes(self):
        self.client.force_authenticate(self.cart_item.user)
        response = self.client.get(factories.CartItemFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue('attributes' in response.data[0])


@ddt.ddt
class CartSubmitTest(test.APITransactionTestCase):
    def setUp(self):
        manager.register(
            offering_type='TEST_TYPE',
            create_resource_processor=utils.TestCreateProcessor,
            can_update_limits=True,
        )
        self.service_settings = structure_factories.ServiceSettingsFactory(
            type='Test', shared=True
        )
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE,
            type='TEST_TYPE',
            scope=self.service_settings,
        )
        self.plan = factories.PlanFactory(offering=self.offering)

    def submit(self, project):
        return self.client.post(
            factories.CartItemFactory.get_list_url('submit'),
            {'project': structure_factories.ProjectFactory.get_url(project)},
        )

    def test_user_can_not_submit_shopping_cart_in_project_without_permissions(self):
        self.client.force_authenticate(self.fixture.user)

        self.client.post(
            factories.CartItemFactory.get_list_url(),
            {
                'offering': factories.OfferingFactory.get_public_url(self.offering),
            },
        )
        response = self.submit(self.fixture.project)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def get_payload(self, project):
        limits = {
            'storage': 1000,
            'ram': 30,
            'cpu_count': 5,
        }

        for key in limits.keys():
            models.OfferingComponent.objects.create(
                offering=self.offering,
                type=key,
                billing_type=models.OfferingComponent.BillingTypes.LIMIT,
            )

        return {
            'offering': factories.OfferingFactory.get_public_url(self.offering),
            'plan': factories.PlanFactory.get_public_url(self.plan),
            'project': structure_factories.ProjectFactory.get_url(project),
            'limits': limits,
            'attributes': {'name': 'test'},
        }

    def test_cart_item_limits_are_propagated_to_order_item(self):
        self.client.force_authenticate(self.fixture.owner)

        url = factories.CartItemFactory.get_list_url()
        payload = self.get_payload(self.fixture.project)
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        response = self.submit(self.fixture.project)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        order_item = models.OrderItem.objects.last()
        self.assertEqual(order_item.limits['cpu_count'], 5)

    def test_plan_validate(self):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.CartItemFactory.get_list_url()
        payload = self.get_payload(self.fixture.project)
        payload.pop('plan')

        self.plan.delete()
        response = self.client.post(url, payload)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

        # if we have only one available plan then plan field is not required
        factories.PlanFactory(offering=self.offering)
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        # if we have few available plans then plan field is required
        factories.PlanFactory(offering=self.offering)
        response = self.client.post(url, payload)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_project_is_validated_when_cart_item_is_created(self):
        self.client.force_authenticate(self.fixture.user)

        url = factories.CartItemFactory.get_list_url()
        payload = self.get_payload(self.fixture.project)
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_limits_are_not_allowed_for_components_with_disabled_quotas(self):
        limits = {
            'storage': 1000,
            'ram': 30,
            'cpu_count': 5,
        }

        plan = factories.PlanFactory(offering=self.offering)

        for key in limits.keys():
            models.OfferingComponent.objects.create(
                offering=self.offering,
                type=key,
                billing_type=models.OfferingComponent.BillingTypes.USAGE,
            )

        payload = {
            'offering': factories.OfferingFactory.get_public_url(self.offering),
            'plan': factories.PlanFactory.get_public_url(plan),
            'limits': limits,
        }

        self.client.force_authenticate(self.fixture.staff)

        url = factories.CartItemFactory.get_list_url()
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @ddt.data(
        models.OfferingComponent.LimitPeriods.TOTAL,
        models.OfferingComponent.LimitPeriods.MONTH,
        models.OfferingComponent.LimitPeriods.ANNUAL,
    )
    def test_offering_limit_is_valid(self, limit_period):
        self.client.force_authenticate(self.fixture.owner)

        url = factories.CartItemFactory.get_list_url()
        payload = self.get_payload(self.fixture.project)
        component = models.OfferingComponent.objects.get(
            offering=self.offering,
            type='cpu_count',
        )
        component.limit_amount = 10
        component.limit_period = limit_period
        component.save()

        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    @ddt.data(
        models.OfferingComponent.LimitPeriods.TOTAL,
        models.OfferingComponent.LimitPeriods.MONTH,
        models.OfferingComponent.LimitPeriods.ANNUAL,
    )
    def test_offering_limit_is_invalid(self, limit_period):
        self.client.force_authenticate(self.fixture.owner)

        url = factories.CartItemFactory.get_list_url()
        payload = self.get_payload(self.fixture.project)
        component = models.OfferingComponent.objects.get(
            offering=self.offering,
            type='cpu_count',
        )
        component.limit_amount = 1
        component.limit_period = limit_period
        component.save()

        response = self.client.post(url, payload)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )


@ddt.ddt
@patch('waldur_mastermind.marketplace.tasks.notify_order_approvers')
class AutoapproveTest(test.APITransactionTestCase):
    def setUp(self):
        manager.register(
            offering_type='TEST_TYPE',
            create_resource_processor=utils.TestCreateProcessor,
        )
        self.service_settings = structure_factories.ServiceSettingsFactory(
            type='Test', shared=True
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_ORDER)
        CustomerRole.OWNER.add_permission(PermissionEnum.APPROVE_PRIVATE_ORDER)
        ProjectRole.MANAGER.add_permission(PermissionEnum.APPROVE_PRIVATE_ORDER)
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_PRIVATE_ORDER)

    def submit(self, project):
        return self.client.post(
            factories.CartItemFactory.get_list_url('submit'),
            {'project': structure_factories.ProjectFactory.get_url(project)},
        )

    def submit_public_and_private(self, role):
        provider_fixture = fixtures.ProjectFixture()
        consumer_fixture = fixtures.ProjectFixture()
        private_offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE,
            shared=False,
            billable=False,
            customer=provider_fixture.customer,
            type='TEST_TYPE',
            scope=self.service_settings,
            project=consumer_fixture.project,
        )
        public_offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE,
            shared=True,
            billable=True,
            customer=provider_fixture.customer,
            type='TEST_TYPE',
            scope=self.service_settings,
        )

        self.client.force_authenticate(getattr(consumer_fixture, role))

        self.client.post(
            factories.CartItemFactory.get_list_url(),
            {
                'offering': factories.OfferingFactory.get_public_url(private_offering),
                'project': structure_factories.ProjectFactory.get_url(
                    consumer_fixture.project
                ),
                'attributes': {'name': 'test'},
                'plan': factories.PlanFactory.get_public_url(
                    factories.PlanFactory(offering=private_offering)
                ),
            },
        )

        self.client.post(
            factories.CartItemFactory.get_list_url(),
            {
                'offering': factories.OfferingFactory.get_public_url(public_offering),
                'project': structure_factories.ProjectFactory.get_url(
                    consumer_fixture.project
                ),
                'attributes': {'name': 'test'},
                'plan': factories.PlanFactory.get_public_url(
                    factories.PlanFactory(offering=public_offering)
                ),
            },
        )

        return self.submit(consumer_fixture.project)

    @ddt.data('staff', 'owner', 'manager', 'admin')
    def test_order_gets_approved_if_all_offerings_are_private(self, role, mocked_task):
        fixture = fixtures.ProjectFixture()
        offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE,
            shared=False,
            billable=False,
            customer=fixture.customer,
            type='TEST_TYPE',
            scope=self.service_settings,
        )

        self.client.force_authenticate(getattr(fixture, role))

        self.client.post(
            factories.CartItemFactory.get_list_url(),
            {
                'offering': factories.OfferingFactory.get_public_url(offering),
                'project': structure_factories.ProjectFactory.get_url(fixture.project),
                'attributes': {'name': 'test'},
                'plan': factories.PlanFactory.get_public_url(
                    factories.PlanFactory(offering=offering)
                ),
            },
        )

        response = self.submit(fixture.project)
        self.assertEqual(response.data['state'], 'executing')
        mocked_task.delay.assert_not_called()

    @ddt.data('staff', 'owner')
    def test_public_offering_is_autoapproved_if_user_is_owner_or_staff(
        self, role, mocked_task
    ):
        response = self.submit_public_and_private(role)
        self.assertEqual(response.data['state'], 'executing')
        mocked_task.delay.assert_not_called()

    @ddt.data('manager', 'admin')
    def test_public_offering_is_not_autoapproved_if_user_is_manager_or_admin(
        self, role, mocked_task
    ):
        response = self.submit_public_and_private(role)
        self.assertEqual(response.data['state'], 'requested for approval')
        mocked_task.delay.assert_called()

    def test_public_offering_is_autoapproved_if_feature_is_enabled_for_manager(
        self, mocked_task
    ):
        ProjectRole.MANAGER.add_permission(PermissionEnum.APPROVE_ORDER)
        response = self.submit_public_and_private('manager')
        self.assertEqual(response.data['state'], 'executing')
        mocked_task.delay.assert_not_called()

    def test_public_offering_is_autoapproved_if_feature_is_enabled_for_admin(
        self, mocked_task
    ):
        ProjectRole.ADMIN.add_permission(PermissionEnum.APPROVE_ORDER)
        response = self.submit_public_and_private('admin')
        self.assertEqual(response.data['state'], 'executing')
        mocked_task.delay.assert_not_called()

    @ddt.data(True, False)
    def test_public_offering_is_approved_in_the_same_organization(
        self, auto_approve_in_service_provider_projects, mocked_task
    ):
        consumer_fixture = provider_fixture = fixtures.ProjectFixture()
        public_offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE,
            shared=True,
            billable=True,
            customer=provider_fixture.customer,
            type='TEST_TYPE',
            scope=self.service_settings,
            plugin_options={
                'auto_approve_in_service_provider_projects': auto_approve_in_service_provider_projects
            },
        )

        self.client.force_authenticate(getattr(consumer_fixture, 'admin'))

        self.client.post(
            factories.CartItemFactory.get_list_url(),
            {
                'offering': factories.OfferingFactory.get_public_url(public_offering),
                'project': structure_factories.ProjectFactory.get_url(
                    consumer_fixture.project
                ),
                'attributes': {'name': 'test'},
                'plan': factories.PlanFactory.get_public_url(
                    factories.PlanFactory(offering=public_offering)
                ),
            },
        )

        response = self.submit(consumer_fixture.project)
        self.assertEqual(
            response.data['state'],
            auto_approve_in_service_provider_projects
            and 'executing'
            or 'requested for approval',
        )
        if auto_approve_in_service_provider_projects:
            mocked_task.delay.assert_not_called()


class CartUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.cart_item = factories.CartItemFactory()

    def update(self, plan):
        self.client.force_authenticate(self.cart_item.user)
        return self.client.patch(
            factories.CartItemFactory.get_url(item=self.cart_item),
            {'plan': factories.PlanFactory.get_public_url(plan)},
        )

    def test_update_cart_item(self):
        new_plan = factories.PlanFactory(offering=self.cart_item.offering)
        response = self.update(new_plan)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_plan_validation(self):
        response = self.update(factories.PlanFactory())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_when_limits_are_updated_estimate_is_recalculated(self):
        # Arrange
        oc = factories.OfferingComponentFactory(
            offering=self.cart_item.offering,
            billing_type=models.OfferingComponent.BillingTypes.LIMIT,
            type='cpu',
        )
        plan = factories.PlanFactory(offering=self.cart_item.offering)
        factories.PlanComponentFactory(
            plan=plan,
            component=oc,
            price=10,
        )
        self.cart_item.limits = {'cpu': 2}
        self.cart_item.plan = plan
        self.cart_item.save()

        # Act
        self.client.force_authenticate(self.cart_item.user)
        url = factories.CartItemFactory.get_url(item=self.cart_item)
        response = self.client.patch(url, {'limits': {'cpu': 4}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.cart_item.refresh_from_db()

        # Assert
        self.assertEqual(self.cart_item.cost, 4 * 10)


class QuotasValidateTest(test.APITransactionTestCase):
    def setUp(self):
        init_args = []
        self.init_args = init_args

        class NewInstanceSerializer(structure_test_serializers.NewInstanceSerializer):
            def __init__(self, *args, **kwargs):
                init_args.extend([self, args, kwargs])
                super().__init__(*args, **kwargs)

            class Meta(structure_test_serializers.NewInstanceSerializer.Meta):
                fields = (
                    structure_test_serializers.NewInstanceSerializer.Meta.fields
                    + ('cores',)
                )

        class TestNewInstanceViewSet(structure_test_views.TestNewInstanceViewSet):
            serializer_class = NewInstanceSerializer

        class TestNewInstanceCreateProcessor(utils.TestCreateProcessor):
            viewset = TestNewInstanceViewSet
            fields = ['name', 'cores']

        manager.register(
            offering_type='TEST_TYPE',
            create_resource_processor=TestNewInstanceCreateProcessor,
        )
        self.service_settings = structure_factories.ServiceSettingsFactory(
            type='Test', shared=True
        )
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            state=models.Offering.States.ACTIVE,
            type='TEST_TYPE',
            scope=self.service_settings,
        )

        structure_models.Project.add_quota_field(
            name='test_cpu_count',
            quota_field=TotalQuotaField(
                target_models=[test_models.TestNewInstance],
                path_to_scope='project',
                target_field='cores',
            ),
        )

        self.fixture.project.set_quota_limit('test_cpu_count', 1)

    def test_cart_item_created_if_quotas_is_valid(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.CartItemFactory.get_list_url(),
            {
                'offering': factories.OfferingFactory.get_public_url(self.offering),
                'project': structure_factories.ProjectFactory.get_url(
                    self.fixture.project
                ),
                'attributes': {'name': 'test', 'cores': 1},
                'plan': factories.PlanFactory.get_public_url(
                    factories.PlanFactory(offering=self.offering)
                ),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @unittest.skip(
        'Consider avoiding service settings quota validation in favor of marketplace offering component limits'
    )
    def test_cart_item_does_not_created_if_quotas_is_not_valid(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.CartItemFactory.get_list_url(),
            {
                'offering': factories.OfferingFactory.get_public_url(self.offering),
                'project': structure_factories.ProjectFactory.get_url(
                    self.fixture.project
                ),
                'attributes': {'name': 'test', 'cores': 2},
                'plan': factories.PlanFactory.get_public_url(
                    factories.PlanFactory(offering=self.offering)
                ),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue('"test_cpu_count" quota is over limit.' in response.data[0])

    def test_context_is_passed_to_serializer(self):
        self.client.force_authenticate(self.fixture.staff)
        self.client.post(
            factories.CartItemFactory.get_list_url(),
            {
                'offering': factories.OfferingFactory.get_public_url(self.offering),
                'project': structure_factories.ProjectFactory.get_url(
                    self.fixture.project
                ),
                'attributes': {'name': 'test', 'cores': 1},
                'plan': factories.PlanFactory.get_public_url(
                    factories.PlanFactory(offering=self.offering)
                ),
            },
        )

        self.assertTrue('context' in self.init_args[2].keys())
