from rest_framework import test

from waldur_core.structure.tests.factories import ProjectFactory
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.tests.factories import InvoiceItemFactory
from waldur_mastermind.invoices.utils import (
    get_current_month_end,
    get_current_month_start,
    get_full_days,
)
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.callbacks import resource_creation_succeeded
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.utils import serialize_resource_limit_period
from waldur_mastermind.marketplace_openstack import CORES_TYPE, TENANT_TYPE
from waldur_mastermind.marketplace_rancher import MANAGED_RANCHER_PLUGIN, PLUGIN_NAME
from waldur_openstack.tests.factories import InstanceFactory, TenantFactory
from waldur_rancher.tests.factories import ClusterFactory, NodeFactory


class RancherInvoiceTest(test.APITransactionTestCase):
    def test_invoice_is_copied_from_tenant_to_cluster(self):
        start = get_current_month_start()
        end = get_current_month_end()

        cluster_project = ProjectFactory()
        cluster = ClusterFactory(project=cluster_project)

        for i in (1, 2):
            vm_project = ProjectFactory()
            vpc = TenantFactory(project=vm_project)
            vm = InstanceFactory(project=vm_project, tenant=vpc)
            vpc_offering = marketplace_factories.OfferingFactory(type=TENANT_TYPE)
            vpc_resource = marketplace_factories.ResourceFactory(
                scope=vpc,
                project=vm_project,
                offering=vpc_offering,
            )
            InvoiceItemFactory(
                resource=vpc_resource,
                project=vm_project,
                unit_price=100,
                unit=marketplace_models.Plan.Units.PER_DAY,
                details={
                    "offering_component_type": CORES_TYPE,
                    "resource_limit_periods": [
                        serialize_resource_limit_period(
                            {"start": start, "end": end, "quantity": 10}
                        )
                    ],
                },
                quantity=get_full_days(start, end) * 10,
                start=start,
                end=end,
                article_code="vpc",
            )

            NodeFactory(cluster=cluster, instance=vm)

        managed_rancher_offering = marketplace_factories.OfferingFactory(
            type=MANAGED_RANCHER_PLUGIN
        )
        rancher_offering = marketplace_factories.OfferingFactory(type=PLUGIN_NAME)
        rancher_plan = marketplace_factories.PlanFactory(
            offering=managed_rancher_offering
        )
        rancher_offering_component = marketplace_factories.OfferingComponentFactory(
            offering=managed_rancher_offering,
            type=CORES_TYPE,
            billing_type=marketplace_models.OfferingComponent.BillingTypes.LIMIT,
            article_code="rancher",
        )
        marketplace_factories.PlanComponentFactory(
            plan=rancher_plan,
            component=rancher_offering_component,
            price=200,
        )

        cluster_resource = marketplace_factories.ResourceFactory(
            offering=rancher_offering,
            plan=rancher_plan,
            state=marketplace_models.Resource.States.CREATING,
            scope=cluster,
            project=cluster_project,
        )

        resource = marketplace_factories.ResourceFactory(
            offering=managed_rancher_offering,
            plan=rancher_plan,
            state=marketplace_models.Resource.States.CREATING,
            scope=cluster_resource,
            project=cluster_project,
        )
        resource_creation_succeeded(resource)

        items = invoices_models.InvoiceItem.objects.filter(resource=resource)

        self.assertEqual(items.count(), 3)

        self.assertTrue(items.filter(unit_price=100).exists())
        self.assertTrue(items.filter(unit_price=200).exists())

        self.assertTrue(items.filter(article_code="vpc").exists())
        self.assertTrue(items.filter(article_code="rancher").exists())
