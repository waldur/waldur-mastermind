from django.core.exceptions import ValidationError
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.common.utils import parse_datetime
from waldur_mastermind.packages.tests import fixtures as packages_fixtures

from .. import models


@freeze_time('2018-11-01')
class DowntimeValidationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = packages_fixtures.PackageFixture()
        self.package = self.fixture.openstack_package
        self.downtime = models.ServiceDowntime.objects.create(
            package=self.package,
            start=parse_datetime('2018-10-05'),
            end=parse_datetime('2018-10-15'),
        )

    def test_positive(self):
        downtime = models.ServiceDowntime(
            package=self.package,
            start=parse_datetime('2018-10-17'),
            end=parse_datetime('2018-10-20'),
        )
        # It is expected that validation error is not raised in this case
        downtime.clean()

    def test_validate_offset(self):
        downtime = models.ServiceDowntime(
            package=self.package,
            start=parse_datetime('2018-11-10'),
            end=parse_datetime('2018-11-20'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_validate_duration(self):
        downtime = models.ServiceDowntime(
            package=self.package,
            start=parse_datetime('2018-10-16'),
            end=parse_datetime('2018-12-20'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_validate_intersection_outside(self):
        downtime = models.ServiceDowntime(
            package=self.package,
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-20'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_validate_intersection_inside(self):
        downtime = models.ServiceDowntime(
            package=self.package,
            start=parse_datetime('2018-10-07'),
            end=parse_datetime('2018-10-10'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_validate_intersection_left(self):
        downtime = models.ServiceDowntime(
            package=self.package,
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-10'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_validate_intersection_right(self):
        downtime = models.ServiceDowntime(
            package=self.package,
            start=parse_datetime('2018-10-10'),
            end=parse_datetime('2018-10-20'),
        )
        self.assertRaises(ValidationError, downtime.clean)


@freeze_time('2018-11-01')
class OpenStackDowntimeAdjustmentTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = packages_fixtures.PackageFixture()
        self.package = self.fixture.openstack_package
        self.item = models.InvoiceItem.objects.get(object_id=self.package.id)
        self.item.start = parse_datetime('2018-10-11')
        self.item.end = parse_datetime('2018-10-15')
        self.item.save()

    def test_downtime_outside_of_invoice_item_billing_period(self):
        models.ServiceDowntime.objects.create(
            package=self.package,
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-20'),
        )
        compensation = models.InvoiceItem.objects.filter(
            start=self.item.start, end=self.item.end, details__icontains='compensation').get()
        self.assertEqual(compensation.price, -1 * self.item.price)
        self.assertEqual(compensation.details['name'],
                         'Compensation for downtime. Resource name: %s' % self.item.name)

    def test_downtime_inside_of_invoice_item_billing_period(self):
        downtime = models.ServiceDowntime.objects.create(
            package=self.package,
            start=parse_datetime('2018-10-12'),
            end=parse_datetime('2018-10-14'),
        )
        self.assertTrue(models.InvoiceItem.objects.filter(
            start=downtime.start, end=downtime.end, details__icontains='compensation').exists())

    def test_downtime_at_the_start_of_invoice_item_billing_period(self):
        downtime = models.ServiceDowntime.objects.create(
            package=self.package,
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-12'),
        )
        self.assertTrue(models.InvoiceItem.objects.filter(
            start=self.item.start, end=downtime.end, details__icontains='compensation').exists())

    def test_downtime_at_the_end_of_invoice_item_billing_period(self):
        downtime = models.ServiceDowntime.objects.create(
            package=self.package,
            start=parse_datetime('2018-10-12'),
            end=parse_datetime('2018-10-20'),
        )
        self.assertTrue(models.InvoiceItem.objects.filter(
            start=downtime.start, end=self.item.end, details__icontains='compensation').exists())

    def test_compensation_is_not_created_if_downtime_and_item_do_not_intersect(self):
        models.ServiceDowntime.objects.create(
            package=self.package,
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-07'),
        )
        self.assertFalse(models.InvoiceItem.objects.filter(scope__isnull=True, details__icontains='compensation')
                         .exists())

    def test_compensation_is_not_created_if_item_does_not_have_package(self):
        self.item.scope = None
        self.item.save()
        models.ServiceDowntime.objects.create(
            package=self.package,
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-20'),
        )
        self.assertFalse(models.InvoiceItem.objects.filter(scope__isnull=True, details__icontains='compensation')
                         .exists())
