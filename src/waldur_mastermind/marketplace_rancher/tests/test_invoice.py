import copy
import datetime
from unittest import mock

from freezegun import freeze_time
from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices import tasks as invoices_tasks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import tasks as marketplace_tasks
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_rancher import PLUGIN_NAME
from waldur_openstack.openstack_tenant.tests import (
    factories as openstack_tenant_factories,
)
from waldur_openstack.openstack_tenant.tests import (
    fixtures as openstack_tenant_fixtures,
)
from waldur_rancher import models as rancher_models
from waldur_rancher import tasks, utils
from waldur_rancher.tests import factories as rancher_factories
from waldur_rancher.tests.utils import backend_node_response


class InvoiceTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = openstack_tenant_fixtures.OpenStackTenantFixture()
        self.patcher = mock.patch(
            'waldur_rancher.backend.RancherBackend.get_cluster_nodes'
        )
        self.mocked_get_cluster_nodes = self.patcher.start()
        self.mocked_get_cluster_nodes.return_value = [
            {'backend_id': 'node_backend_id', 'name': 'name-rancher-node-1'}
        ]

        self.patcher_client = mock.patch('waldur_rancher.backend.RancherBackend.client')
        self.mock_client = self.patcher_client.start()
        self.mock_client.get_node.return_value = backend_node_response

        service = rancher_factories.RancherServiceFactory(
            customer=self.fixture.customer
        )
        spl = rancher_factories.RancherServiceProjectLinkFactory(
            project=self.fixture.project, service=service
        )
        service_settings = spl.service.settings
        self.offering = marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME, scope=service_settings
        )
        self.plan = marketplace_factories.PlanFactory(offering=self.offering,)
        self.offering_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type='node',
            billing_type=marketplace_models.OfferingComponent.BillingTypes.USAGE,
        )
        self.plan_component = marketplace_factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component,
        )
        openstack_tenant_factories.FlavorFactory(
            settings=self.fixture.spl.service.settings, ram=1024 * 8, cores=8
        )
        image = openstack_tenant_factories.ImageFactory(
            settings=self.fixture.spl.service.settings
        )
        openstack_tenant_factories.SecurityGroupFactory(
            name='default', settings=self.fixture.spl.service.settings
        )
        service_settings.options['base_image_name'] = image.name
        service_settings.save()

        self.resource = None
        self.cluster = None
        self.plan_period = None

    def tearDown(self):
        super(InvoiceTest, self).tearDown()
        mock.patch.stopall()

    def _create_usage(self, mock_executors):
        order = marketplace_factories.OrderFactory(
            project=self.fixture.project, created_by=self.fixture.owner
        )
        order_item = marketplace_factories.OrderItemFactory(
            order=order,
            offering=self.offering,
            attributes={
                'name': 'name',
                'tenant_settings': openstack_tenant_factories.OpenStackTenantServiceSettingsFactory.get_url(
                    self.fixture.openstack_tenant_service_settings
                ),
                'nodes': [
                    {
                        'subnet': openstack_tenant_factories.SubNetFactory.get_url(
                            self.fixture.subnet
                        ),
                        'system_volume_size': 1024,
                        'memory': 1,
                        'cpu': 1,
                        'roles': ['controlplane', 'etcd', 'worker'],
                    }
                ],
            },
        )
        serialized_order = core_utils.serialize_instance(order_item.order)
        serialized_user = core_utils.serialize_instance(self.fixture.staff)
        marketplace_tasks.process_order(serialized_order, serialized_user)
        self.assertTrue(
            marketplace_models.Resource.objects.filter(name='name').exists()
        )
        self.assertTrue(rancher_models.Cluster.objects.filter(name='name').exists())

        self.cluster = rancher_models.Cluster.objects.get(name='name')
        self.cluster.backend_id = 'cluster_backend_id'
        self.cluster.save()

        create_node_task = tasks.CreateNodeTask()
        create_node_task.execute(
            mock_executors.ClusterCreateExecutor.execute.mock_calls[0][1][
                0
            ].node_set.first(),
            user_id=mock_executors.ClusterCreateExecutor.execute.mock_calls[0][2][
                'user'
            ].id,
        )
        self.assertTrue(self.cluster.node_set.filter(cluster=self.cluster).exists())

        today = datetime.date.today()
        self.resource = marketplace_models.Resource.objects.get(scope=self.cluster)
        self.plan_period = marketplace_models.ResourcePlanPeriod.objects.create(
            start=today, end=None, resource=self.resource, plan=self.plan,
        )
        invoices_tasks.create_monthly_invoices()
        tasks.pull_cluster_nodes(self.cluster.id)
        utils.update_cluster_nodes_states(self.cluster.id)

    @freeze_time('2019-01-01')
    @mock.patch('waldur_rancher.views.executors')
    def test_create_usage_if_node_is_active(self, mock_executors):
        self._create_usage(mock_executors)
        today = datetime.date.today()
        self.assertTrue(
            marketplace_models.ComponentUsage.objects.filter(
                resource=self.resource,
                component=self.offering_component,
                usage=1,
                date=today,
                billing_period=today,
                plan_period=self.plan_period,
            ).exists()
        )
        invoice = invoices_models.Invoice.objects.get(customer=self.cluster.customer)
        self.assertEqual(invoice.items.count(), 1)
        self.assertEqual(invoice.price, self.plan_component.price)

    @freeze_time('2019-01-01')
    @mock.patch('waldur_rancher.views.executors')
    def test_usage_is_zero_if_node_is_not_active(self, mock_executors):
        return_value = copy.copy(self.mock_client.get_node.return_value)
        return_value['state'] = 'error'
        self.mock_client.get_node.return_value = return_value
        self._create_usage(mock_executors)
        today = datetime.date.today()
        self.assertTrue(
            marketplace_models.ComponentUsage.objects.filter(
                resource=self.resource,
                component=self.offering_component,
                date=today,
                billing_period=today,
                plan_period=self.plan_period,
            ).exists()
        )
        usage = marketplace_models.ComponentUsage.objects.get(
            resource=self.resource,
            component=self.offering_component,
            date=today,
            billing_period=today,
            plan_period=self.plan_period,
        )
        self.assertEqual(usage.usage, 0)

    @freeze_time('2019-01-01')
    @mock.patch('waldur_rancher.views.executors')
    def test_usage_grows_if_active_nodes_count_grow(self, mock_executors):
        self._create_usage(mock_executors)
        today = datetime.date.today()
        self.assertTrue(
            marketplace_models.ComponentUsage.objects.filter(
                resource=self.resource,
                component=self.offering_component,
                usage=1,
                date=today,
                billing_period=today,
                plan_period=self.plan_period,
            ).exists()
        )
        rancher_factories.NodeFactory(cluster=self.cluster, name='second node')
        self.mocked_get_cluster_nodes.return_value = [
            {'backend_id': 'node_backend_id', 'name': 'name-rancher-node'},
            {'backend_id': 'second_node_backend_id', 'name': 'second node'},
        ]
        tasks.pull_cluster_nodes(self.cluster.id)
        utils.update_cluster_nodes_states(self.cluster.id)
        self.assertTrue(
            marketplace_models.ComponentUsage.objects.filter(
                resource=self.resource,
                component=self.offering_component,
                usage=2,
                date=today,
                billing_period=today,
                plan_period=self.plan_period,
            ).exists()
        )
        self.assertEqual(
            marketplace_models.ComponentUsage.objects.filter(
                resource=self.resource,
                component=self.offering_component,
                date=today,
                billing_period=today,
                plan_period=self.plan_period,
            ).count(),
            1,
        )
        invoice = invoices_models.Invoice.objects.get(customer=self.cluster.customer)
        self.assertEqual(invoice.items.count(), 1)
        self.assertEqual(invoice.price, self.plan_component.price * 2)

    @freeze_time('2019-01-01')
    @mock.patch('waldur_rancher.views.executors')
    def test_usage_does_not_decrease_if_active_nodes_count_decrease(
        self, mock_executors
    ):
        self._create_usage(mock_executors)
        today = datetime.date.today()
        self.assertTrue(
            marketplace_models.ComponentUsage.objects.filter(
                resource=self.resource,
                component=self.offering_component,
                usage=1,
                date=today,
                billing_period=today,
                plan_period=self.plan_period,
            ).exists()
        )
        rancher_factories.NodeFactory(cluster=self.cluster, name='second node')
        self.mocked_get_cluster_nodes.return_value = [
            {'backend_id': 'node_backend_id', 'name': 'name-rancher-node'},
            {'backend_id': 'second_node_backend_id', 'name': 'second node'},
        ]
        tasks.pull_cluster_nodes(self.cluster.id)
        utils.update_cluster_nodes_states(self.cluster.id)
        self.assertTrue(
            marketplace_models.ComponentUsage.objects.filter(
                resource=self.resource,
                component=self.offering_component,
                usage=2,
                date=today,
                billing_period=today,
                plan_period=self.plan_period,
            ).exists()
        )
        return_value = copy.copy(self.mock_client.get_node.return_value)
        return_value['state'] = 'error'
        self.mock_client.get_node.return_value = return_value
        tasks.pull_cluster_nodes(self.cluster.id)
        self.assertTrue(
            marketplace_models.ComponentUsage.objects.filter(
                resource=self.resource,
                component=self.offering_component,
                usage=2,
                date=today,
                billing_period=today,
                plan_period=self.plan_period,
            ).exists()
        )

        invoice = invoices_models.Invoice.objects.get(customer=self.cluster.customer)
        self.assertEqual(invoice.items.count(), 1)
        self.assertEqual(invoice.price, self.plan_component.price * 2)
