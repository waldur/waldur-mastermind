from django.core.exceptions import ObjectDoesNotExist, ValidationError
from dateutil import parser
from django.utils.timezone import get_current_timezone
from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.packages.tests import fixtures as packages_fixtures

from .. import models


def parse_datetime(timestr):
    return parser.parse(timestr).replace(tzinfo=get_current_timezone())


@freeze_time('2018-11-01')
class DowntimeValidationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = packages_fixtures.PackageFixture()
        self.settings = self.fixture.openstack_package.service_settings
        self.downtime = models.ServiceDowntime.objects.create(
            settings=self.settings,
            start=parse_datetime('2018-10-05'),
            end=parse_datetime('2018-10-15'),
        )

    def test_positive(self):
        downtime = models.ServiceDowntime(
            settings=self.settings,
            start=parse_datetime('2018-10-17'),
            end=parse_datetime('2018-10-20'),
        )
        # It is expected that validation error is not raised in this case
        downtime.clean()

    def test_validate_offset(self):
        downtime = models.ServiceDowntime(
            settings=self.settings,
            start=parse_datetime('2018-11-10'),
            end=parse_datetime('2018-11-20'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_validate_duration(self):
        downtime = models.ServiceDowntime(
            settings=self.settings,
            start=parse_datetime('2018-10-16'),
            end=parse_datetime('2018-12-20'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_validate_intersection_outside(self):
        downtime = models.ServiceDowntime(
            settings=self.settings,
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-20'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_validate_intersection_inside(self):
        downtime = models.ServiceDowntime(
            settings=self.settings,
            start=parse_datetime('2018-10-07'),
            end=parse_datetime('2018-10-10'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_validate_intersection_left(self):
        downtime = models.ServiceDowntime(
            settings=self.settings,
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-10'),
        )
        self.assertRaises(ValidationError, downtime.clean)

    def test_validate_intersection_right(self):
        downtime = models.ServiceDowntime(
            settings=self.settings,
            start=parse_datetime('2018-10-10'),
            end=parse_datetime('2018-10-20'),
        )
        self.assertRaises(ValidationError, downtime.clean)


@freeze_time('2018-11-01')
class OpenStackDowntimeAdjustmentTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = packages_fixtures.PackageFixture()
        self.package = self.fixture.openstack_package
        self.package_settings = self.package.service_settings
        self.item = models.OpenStackItem.objects.get(package=self.package)

    def test_downtime_outside_of_invoice_item_billing_period(self):
        self.item.start = parse_datetime('2018-10-11')
        self.item.end = parse_datetime('2018-10-15')
        self.item.save()
        models.ServiceDowntime.objects.create(
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-20'),
            settings=self.package_settings
        )
        self.assertRaises(ObjectDoesNotExist, self.item.refresh_from_db)

    def test_downtime_inside_of_invoice_item_billing_period(self):
        self.item.start = parse_datetime('2018-10-01')
        self.item.end = parse_datetime('2018-10-20')
        self.item.save()
        models.ServiceDowntime.objects.create(
            start=parse_datetime('2018-10-11'),
            end=parse_datetime('2018-10-15'),
            settings=self.package_settings
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.start, parse_datetime('2018-10-01'))
        self.assertEqual(self.item.end, parse_datetime('2018-10-11'))
        self.assertEqual(models.OpenStackItem.objects.get(start='2018-10-15').end, parse_datetime('2018-10-20'))

    def test_downtime_at_the_start_of_invoice_item_billing_period(self):
        self.item.start = parse_datetime('2018-10-11')
        self.item.end = parse_datetime('2018-10-15')
        self.item.save()
        models.ServiceDowntime.objects.create(
            start=parse_datetime('2018-10-01'),
            end=parse_datetime('2018-10-12'),
            settings=self.package_settings
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.start, parse_datetime('2018-10-12'))

    def test_downtime_at_the_end_of_invoice_item_billing_period(self):
        self.item.start = parse_datetime('2018-10-11')
        self.item.end = parse_datetime('2018-10-15')
        self.item.save()
        models.ServiceDowntime.objects.create(
            start=parse_datetime('2018-10-12'),
            end=parse_datetime('2018-10-20'),
            settings=self.package_settings
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.end, parse_datetime('2018-10-12'))
