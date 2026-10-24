from django.test import TestCase

from waldur_core.core.models import CoreStates
from waldur_core.structure.models import ServiceSettings
from waldur_core.structure.tests import factories
from waldur_mastermind.support.models import Issue
from waldur_mastermind.support.tests import factories as support_factories


class StateMixinSaveTest(TestCase):
    """How StateMixin models save today, without optimistic locking (#866).

    django_fsm's ConcurrentTransitionMixin was listed on StateMixin but never
    ran. Turning it on makes every case below fail, so these tests pin the
    behaviour until #866 enables it deliberately.
    """

    def test_transition_out_of_erred_saves(self):
        # Leaving ERRED clears the error message in a post_transition handler,
        # which saves the object in the middle of the transition.
        settings = factories.ServiceSettingsFactory(
            state=CoreStates.ERRED, error_message="Backend is unreachable"
        )
        settings = ServiceSettings.objects.get(pk=settings.pk)

        settings.recover()
        settings.save(update_fields=["state"])

        settings.refresh_from_db()
        self.assertEqual(settings.state, CoreStates.OK)
        self.assertEqual(settings.error_message, "")

    def test_scheduling_from_erred_saves(self):
        settings = factories.ServiceSettingsFactory(
            state=CoreStates.ERRED, error_message="Backend is unreachable"
        )
        settings = ServiceSettings.objects.get(pk=settings.pk)

        settings.schedule_updating()
        settings.save()

        settings.refresh_from_db()
        self.assertEqual(settings.state, CoreStates.UPDATE_SCHEDULED)

    def test_deferred_querysets_load(self):
        factories.ServiceSettingsFactory.create_batch(3)
        support_factories.IssueFactory.create_batch(3)

        self.assertEqual(len(list(ServiceSettings.objects.only("uuid"))), 3)
        self.assertEqual(len(list(Issue.objects.defer("state"))), 3)

    def test_stale_copy_overwrites_newer_state(self):
        settings = factories.ServiceSettingsFactory(state=CoreStates.OK)
        first = ServiceSettings.objects.get(pk=settings.pk)
        second = ServiceSettings.objects.get(pk=settings.pk)

        first.schedule_updating()
        first.save()
        second.schedule_deleting()
        second.save()

        settings.refresh_from_db()
        self.assertEqual(settings.state, CoreStates.DELETION_SCHEDULED)
