import datetime
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from waldur_core.core.enums import CoreStates
from waldur_openstack.tests import factories

MISSING_MESSAGE = "Does not exist at backend."


class ListMissingResourcesCommandTest(TestCase):
    def call_command(self, **options):
        output = StringIO()
        call_command("list_missing_resources", stdout=output, **options)
        return output.getvalue()

    def test_resource_missing_for_long_enough_is_reported(self):
        instance = factories.InstanceFactory(
            state=CoreStates.ERRED,
            error_message=MISSING_MESSAGE,
            backend_missing_since=timezone.now() - datetime.timedelta(days=30),
        )
        output = self.call_command(days=7)
        self.assertIn(str(instance.uuid), output)
        self.assertIn("30d", output)

    def test_recently_missing_resource_is_not_reported(self):
        instance = factories.InstanceFactory(
            state=CoreStates.ERRED,
            error_message=MISSING_MESSAGE,
            backend_missing_since=timezone.now() - datetime.timedelta(days=1),
        )
        output = self.call_command(days=7)
        self.assertNotIn(str(instance.uuid), output)

    def test_healthy_resource_is_not_reported(self):
        instance = factories.InstanceFactory(state=CoreStates.OK)
        output = self.call_command(days=7)
        self.assertNotIn(str(instance.uuid), output)

    def test_resource_erred_for_another_reason_is_not_reported(self):
        instance = factories.InstanceFactory(
            state=CoreStates.ERRED,
            error_message="Failed to provision.",
            backend_missing_since=timezone.now() - datetime.timedelta(days=30),
        )
        output = self.call_command(days=7)
        self.assertNotIn(str(instance.uuid), output)

    def test_resource_without_backend_id_is_not_reported(self):
        instance = factories.InstanceFactory(
            state=CoreStates.ERRED,
            error_message=MISSING_MESSAGE,
            backend_id="",
            backend_missing_since=timezone.now() - datetime.timedelta(days=30),
        )
        output = self.call_command(days=7)
        self.assertNotIn(str(instance.uuid), output)

    def test_resource_marked_before_tracking_is_reported_with_unknown_age(self):
        volume = factories.VolumeFactory(
            state=CoreStates.ERRED,
            error_message=MISSING_MESSAGE,
            backend_missing_since=None,
        )
        output = self.call_command(days=7)
        self.assertIn(str(volume.uuid), output)
        self.assertIn("unknown", output)
