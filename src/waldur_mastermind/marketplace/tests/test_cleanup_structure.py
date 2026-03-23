from django.core.management import call_command
from django.test import TestCase, TransactionTestCase

from waldur_core.core import models as core_models
from waldur_core.logging import models as logging_models
from waldur_core.structure.tests import factories as structure_factories


class CleanupStructureStandardTest(TestCase):
    """Test that standard (non-fast) cleanup handles FK dependencies on core_user."""

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.other_user = structure_factories.UserFactory()

    def test_cleanup_deletes_user_data_access_logs(self):
        logging_models.UserDataAccessLog.objects.create(
            target_user=self.user,
            accessor=self.other_user,
            accessor_type=logging_models.UserDataAccessLog.AccessorType.STAFF,
            accessed_fields=["email"],
        )
        call_command("cleanup_structure")
        self.assertEqual(core_models.User.all_objects.count(), 0)
        self.assertEqual(logging_models.UserDataAccessLog.objects.count(), 0)

    def test_cleanup_handles_all_user_dependencies(self):
        logging_models.UserDataAccessLog.objects.create(
            target_user=self.user,
            accessor=self.other_user,
            accessor_type=logging_models.UserDataAccessLog.AccessorType.SELF,
            accessed_fields=["username"],
        )
        logging_models.WebHook.objects.create(
            user=self.user,
            destination_url="https://example.com/hook",
            event_types=[],
        )
        logging_models.EmailHook.objects.create(
            user=self.user,
            email="test@example.com",
            event_types=[],
        )
        logging_models.EventSubscription.objects.create(
            user=self.user,
        )
        call_command("cleanup_structure")
        self.assertEqual(core_models.User.all_objects.count(), 0)


class CleanupStructureFastTest(TransactionTestCase):
    """Test that fast (TRUNCATE/DELETE) cleanup handles FK dependencies on core_user.

    Uses TransactionTestCase because TRUNCATE cannot run inside a test transaction.
    """

    def setUp(self):
        self.user = structure_factories.UserFactory()
        self.other_user = structure_factories.UserFactory()

    def test_fast_cleanup_deletes_user_data_access_logs(self):
        logging_models.UserDataAccessLog.objects.create(
            target_user=self.user,
            accessor=self.other_user,
            accessor_type=logging_models.UserDataAccessLog.AccessorType.STAFF,
            accessed_fields=["email"],
        )
        call_command("cleanup_structure", "--fast")
        self.assertEqual(core_models.User.all_objects.count(), 0)
        self.assertEqual(logging_models.UserDataAccessLog.objects.count(), 0)

    def test_fast_cleanup_handles_all_user_dependencies(self):
        logging_models.UserDataAccessLog.objects.create(
            target_user=self.user,
            accessor=self.other_user,
            accessor_type=logging_models.UserDataAccessLog.AccessorType.SELF,
            accessed_fields=["username"],
        )
        logging_models.WebHook.objects.create(
            user=self.user,
            destination_url="https://example.com/hook",
            event_types=[],
        )
        logging_models.EmailHook.objects.create(
            user=self.user,
            email="test@example.com",
            event_types=[],
        )
        logging_models.EventSubscription.objects.create(
            user=self.user,
        )
        call_command("cleanup_structure", "--fast")
        self.assertEqual(core_models.User.all_objects.count(), 0)
