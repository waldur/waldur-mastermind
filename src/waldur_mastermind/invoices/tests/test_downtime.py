from django.core.exceptions import ValidationError
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.common.utils import parse_datetime
from waldur_mastermind.invoices import models, tasks
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import PACKAGE_TYPE
from waldur_mastermind.packages.tests.utils import override_plugin_settings


@freeze_time('2018-11-01')
class DowntimeValidationTest(test.APITransactionTestCase):
    def setUp(self):
        self.resource = marketplace_factories.ResourceFactory()
        self.downtime = models.ServiceDowntime.objects.create(
            resource=self.resource,
            start=parse_datetime('2018-10-05'),
            end=parse_datetime('2018-10-15'),
        )

    def test_positive(self):
        downtime = models.ServiceDowntime(
            resource=self.resource,
            start=parse_datetime('2018-10-17'),
            end=parse_datetime('2018-10-20'),
        )
        # It is expected that validation error is not raised in this case
        downtime.clean()

    def test_validate_offset(self):
        downtime = models.ServiceDowntime(
            resource=self.resource,
            start=parse_datetime('2018-11-10'),
            end=parse_datetime('2018-11-20'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_validate_duration(self):
        downtime = models.ServiceDowntime(
            resource=self.resource,
            start=parse_datetime('2018-10-16'),
            end=parse_datetime('2018-12-20'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_validate_intersection_outside(self):
        downtime = models.ServiceDowntime(
            resource=self.resource,
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-20'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_validate_intersection_inside(self):
        downtime = models.ServiceDowntime(
            resource=self.resource,
            start=parse_datetime('2018-10-07'),
            end=parse_datetime('2018-10-10'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_validate_intersection_left(self):
        downtime = models.ServiceDowntime(
            resource=self.resource,
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-10'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_validate_intersection_right(self):
        downtime = models.ServiceDowntime(
            resource=self.resource,
            start=parse_datetime('2018-10-10'),
            end=parse_datetime('2018-10-20'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_offering_or_resource_must_be_defined(self):
        downtime = models.ServiceDowntime(
            start=parse_datetime('2018-10-10'), end=parse_datetime('2018-10-20'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_offering_and_resource_cannot_be_defined(self):
        downtime = models.ServiceDowntime(
            resource=self.resource,
            offering=self.resource.offering,
            start=parse_datetime('2018-10-10'),
            end=parse_datetime('2018-10-20'),
        )
        self.assertRaises(ValidationError, downtime.clean)


@freeze_time('2018-11-01')
@override_plugin_settings(BILLING_ENABLED=True)
class ResourceDowntimeAdjustmentTest(test.APITransactionTestCase):
    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory(type=PACKAGE_TYPE,)
        self.offering_component = marketplace_factories.OfferingComponentFactory(
            offering=self.offering
        )
        self.plan = marketplace_factories.PlanFactory(offering=self.offering)
        self.component = marketplace_factories.PlanComponentFactory(
            component=self.offering_component
        )
        self.resource = marketplace_factories.ResourceFactory(
            state=marketplace_models.Resource.States.OK,
            offering=self.offering,
            plan=self.plan,
        )
        tasks.create_monthly_invoices()
        self.item = models.InvoiceItem.objects.get(object_id=self.resource.id)
        self.item.start = parse_datetime('2018-10-11')
        self.item.end = parse_datetime('2018-10-15')
        self.item.save()

    def test_downtime_outside_of_invoice_item_billing_period(self):
        models.ServiceDowntime.objects.create(
            resource=self.resource,
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-20'),
        )
        compensation = models.InvoiceItem.objects.filter(
            start=self.item.start, end=self.item.end, details__icontains='compensation'
        ).get()
        self.assertEqual(compensation.price, -1 * self.item.price)
        self.assertEqual(
            compensation.details['name'], 'Compensation. %s' % self.item.name,
        )

    def test_downtime_inside_of_invoice_item_billing_period(self):
        downtime = models.ServiceDowntime.objects.create(
            resource=self.resource,
            start=parse_datetime('2018-10-12'),
            end=parse_datetime('2018-10-14'),
        )
        self.assertTrue(
            models.InvoiceItem.objects.filter(
                start=downtime.start,
                end=downtime.end,
                details__icontains='compensation',
            ).exists()
        )

    def test_downtime_at_the_start_of_invoice_item_billing_period(self):
        downtime = models.ServiceDowntime.objects.create(
            resource=self.resource,
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-12'),
        )
        self.assertTrue(
            models.InvoiceItem.objects.filter(
                start=self.item.start,
                end=downtime.end,
                details__icontains='compensation',
            ).exists()
        )

    def test_downtime_at_the_end_of_invoice_item_billing_period(self):
        downtime = models.ServiceDowntime.objects.create(
            resource=self.resource,
            start=parse_datetime('2018-10-12'),
            end=parse_datetime('2018-10-20'),
        )
        self.assertTrue(
            models.InvoiceItem.objects.filter(
                start=downtime.start,
                end=self.item.end,
                details__icontains='compensation',
            ).exists()
        )

    def test_compensation_is_not_created_if_downtime_and_item_do_not_intersect(self):
        models.ServiceDowntime.objects.create(
            resource=self.resource,
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-07'),
        )
        self.assertFalse(
            models.InvoiceItem.objects.filter(
                scope__isnull=True, details__icontains='compensation'
            ).exists()
        )

    def test_compensation_is_not_created_if_item_does_not_have_package(self):
        self.item.scope = None
        self.item.save()
        models.ServiceDowntime.objects.create(
            resource=self.resource,
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-20'),
        )
        self.assertFalse(
            models.InvoiceItem.objects.filter(
                scope__isnull=True, details__icontains='compensation'
            ).exists()
        )

    def test_delete_compensation_if_downtime_has_been_deleted(self):
        downtime = models.ServiceDowntime.objects.create(
            resource=self.resource,
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-20'),
        )
        compensation = models.InvoiceItem.objects.filter(
            start=self.item.start, end=self.item.end, details__icontains='compensation'
        ).get()
        downtime.delete()
        self.assertRaises(models.InvoiceItem.DoesNotExist, compensation.refresh_from_db)
