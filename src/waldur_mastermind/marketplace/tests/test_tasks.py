import datetime

from django.core import mail
from rest_framework import test

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import exceptions, models, tasks

from . import factories


class CalculateUsageForCurrentMonthTest(test.APITransactionTestCase):
    def setUp(self):
        offering = factories.OfferingFactory()
        plan = factories.PlanFactory(offering=offering)
        resource = factories.ResourceFactory(offering=offering)
        category_component = factories.CategoryComponentFactory()
        self.offering_component = factories.OfferingComponentFactory(
            offering=offering,
            parent=category_component,
            billing_type=models.OfferingComponent.BillingTypes.USAGE,
        )
        factories.PlanComponentFactory(plan=plan, component=self.offering_component)
        plan_period = models.ResourcePlanPeriod.objects.create(
            resource=resource, plan=plan, start=datetime.datetime.now()
        )
        models.ComponentUsage.objects.create(
            resource=resource,
            component=self.offering_component,
            usage=10,
            date=datetime.datetime.now(),
            billing_period=core_utils.month_start(datetime.datetime.now()),
            plan_period=plan_period,
        )

    def test_calculate_usage_if_category_component_is_set(self):
        tasks.calculate_usage_for_current_month()
        self.assertEqual(models.CategoryComponentUsage.objects.count(), 2)

    def test_calculate_usage_if_category_component_is_not_set(self):
        self.offering_component.parent = None
        self.offering_component.save()
        tasks.calculate_usage_for_current_month()
        self.assertEqual(models.CategoryComponentUsage.objects.count(), 0)


class NotificationTest(test.APITransactionTestCase):
    def test_notify_about_resource_change(self):
        project_fixture = structure_fixtures.ProjectFixture()
        admin = project_fixture.admin
        project = project_fixture.project
        resource = factories.ResourceFactory(project=project, name='Test resource')
        tasks.notify_about_resource_change(
            'marketplace_resource_create_succeeded',
            {'resource_name': resource.name},
            resource.uuid,
        )
        self.assertEqual(len(mail.outbox), 1)
        subject_template_name = '%s/%s_subject.txt' % (
            'marketplace',
            'marketplace_resource_create_succeeded',
        )
        subject = core_utils.format_text(
            subject_template_name, {'resource_name': resource.name}
        )
        self.assertEqual(mail.outbox[0].subject, subject)
        self.assertEqual(mail.outbox[0].to[0], admin.email)
        self.assertTrue(resource.name in mail.outbox[0].body)
        self.assertTrue(resource.name in mail.outbox[0].subject)


class TerminateResource(test.APITransactionTestCase):
    def setUp(self):
        fixture = structure_fixtures.UserFixture()
        self.user = fixture.staff
        offering = factories.OfferingFactory()
        self.resource = factories.ResourceFactory(offering=offering)
        factories.OrderItemFactory(
            resource=self.resource,
            type=models.OrderItem.Types.TERMINATE,
            state=models.OrderItem.States.EXECUTING,
        )

    def test_raise_exception_if_order_item_has_not_been_created(self):
        self.assertRaises(
            exceptions.ResourceTerminateException,
            tasks.terminate_resource,
            core_utils.serialize_instance(self.resource),
            core_utils.serialize_instance(self.user),
        )
