import datetime

from ddt import data, ddt
from django.core import mail
from django.test import override_settings
from rest_framework import status, test

from waldur_core.permissions.fixtures import CallRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, tasks
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import fixtures

from . import factories


@ddt
class PublicRoundTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()

    @data(
        "staff",
        "owner",
        "user",
        "customer_support",
    )
    def test_rounds_should_be_visible_to_all_authenticated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.CallFactory.get_public_url(self.fixture.call)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()["rounds"]), 1)

    def test_rounds_should_be_visible_to_unauthenticated_users(
        self,
    ):
        url = factories.CallFactory.get_public_url(self.fixture.call)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()["rounds"]), 1)


@ddt
class RoundGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.RoundFactory.get_list_url(self.fixture.call)

    @data(
        "staff",
        "call_manager",
        "call_organizer_user",
    )
    def test_round_should_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_round_should_not_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class RoundCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.round = self.fixture.round
        self.round.start_time = datetime.date.today() - datetime.timedelta(days=10)
        self.round.cutoff_time = datetime.date.today() - datetime.timedelta(days=5)
        self.round.save()
        self.url = factories.RoundFactory.get_list_url(self.fixture.call)

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_add_round_to_call(self, user):
        response = self.create_round(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Round.objects.filter(uuid=response.data["uuid"]).exists()
        )

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_user_can_not_add_offering_to_call(self, user):
        response = self.create_round(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_overlapping_of_rounds(self):
        # old: ---[-]-------
        # new: --------[-]--
        response = self.create_round("staff")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        models.Round.objects.filter(uuid=response.data["uuid"]).delete()

        # old: ---------[-]-
        # new: --------[-]--
        self.round.start_time = datetime.date.today() + datetime.timedelta(days=1)
        self.round.cutoff_time = datetime.date.today() + datetime.timedelta(days=2)
        self.round.save()
        response = self.create_round("staff")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # old: -------[-]---
        # new: --------[-]--
        self.round.start_time = datetime.date.today() - datetime.timedelta(days=1)
        self.round.cutoff_time = datetime.date.today() + datetime.timedelta(days=1)
        self.round.save()
        response = self.create_round("staff")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # old: -------[---]-
        # new: --------[-]--
        self.round.start_time = datetime.date.today() - datetime.timedelta(days=1)
        self.round.cutoff_time = datetime.date.today() + datetime.timedelta(days=3)
        self.round.save()
        response = self.create_round("staff")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # old: ---------[]---
        # new: --------[--]--
        self.round.start_time = datetime.date.today() + datetime.timedelta(days=1)
        self.round.cutoff_time = datetime.date.today() + datetime.timedelta(days=1)
        self.round.save()
        response = self.create_round("staff")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # old: ------------[-]
        # new: --------[-]----
        self.round.start_time = datetime.date.today() + datetime.timedelta(days=3)
        self.round.cutoff_time = datetime.date.today() + datetime.timedelta(days=4)
        self.round.save()
        response = self.create_round("staff")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def create_round(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "start_time": (datetime.date.today()).strftime("%Y-%m-%dT%H:%M:%S"),
            "cutoff_time": (
                datetime.date.today() + datetime.timedelta(days=2)
            ).strftime("%Y-%m-%dT%H:%M:%S"),
            "review_strategy": models.Round.ReviewStrategies.AFTER_PROPOSAL,
            "deciding_entity": models.Round.AllocationStrategies.BY_CALL_MANAGER,
            "review_duration_in_days": 2,
            "minimum_number_of_reviewers": 3,
            "minimal_average_scoring": 3.0,
            "allocation_date": (
                datetime.date.today() + datetime.timedelta(days=2)
            ).strftime("%Y-%m-%dT%H:%M:%S"),
        }

        return self.client.post(self.url, payload)


@ddt
class RoundUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.round = self.fixture.round
        self.url = factories.RoundFactory.get_url(self.fixture.call, self.round)

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_update_round(self, user):
        response = self.update_round(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_user_can_not_update_round(self, user):
        response = self.update_round(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def update_round(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        payload = {
            "start_time": datetime.date.today().strftime("%Y-%m-%dT%H:%M:%S"),
            "cutoff_time": (
                datetime.date.today() + datetime.timedelta(days=3)
            ).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        response = self.client.patch(self.url, payload)
        self.round.refresh_from_db()
        return response


@ddt
class RoundDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.round = self.fixture.new_round
        self.url = factories.RoundFactory.get_url(self.fixture.call, self.round)

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_delete_round(self, user):
        response = self.delete_round(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_user_can_not_delete_round(self, user):
        response = self.delete_round(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def delete_round(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        return self.client.delete(self.url)


@ddt
class RoundCloseTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.round = self.fixture.new_round
        self.round.minimum_number_of_reviewers = 1
        self.round.save()
        self.url = factories.RoundFactory.get_url(
            self.fixture.call, self.round, "close"
        )
        self.proposal = factories.ProposalFactory(
            round=self.round,
            state=ProposalStates.SUBMITTED,
            project=self.fixture.proposal_project,
        )

    @data(
        "staff",
        "call_manager",
    )
    def test_user_can_close_round(self, user):
        self.assertEqual(self.proposal.review_set.count(), 0)
        response = self.close_round(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.review_set.count(), 1)

    @data(
        "user",
        "owner",
        "customer_support",
    )
    def test_user_can_not_close_round(self, user):
        response = self.close_round(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def close_round(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        return self.client.post(self.url)


class RoundNotificationsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.round = self.fixture.round
        self.reviewer_1 = self.fixture.reviewer_1
        self.reviewer_2 = self.fixture.reviewer_2
        self.call = self.fixture.call
        self.call_manager = self.fixture.call_manager
        self.call.add_user(self.call_manager, CallRole.MANAGER)

        # set the other round in another time not to trigger notification
        self.fixture.new_round.start_time = datetime.date.today() + datetime.timedelta(
            days=2
        )
        self.fixture.new_round.save()

    @override_settings(task_always_eager=True)
    def test_reviewer_is_notified_on_round_start(self):
        structure_factories.NotificationFactory(
            key="proposal.round_opening_for_reviewers",
        )
        self.assertTrue(self.round.call.reviewers.count())
        tasks.notify_reviewer_on_round_start()
        self.assertEqual(len(mail.outbox), 2)

        self.assertIn(self.reviewer_1.email, mail.outbox[0].to)
        self.assertIn(self.reviewer_2.email, mail.outbox[1].to)
        self.assertIn(self.call.name, mail.outbox[0].subject, mail.outbox[1].subject)

        body_1 = mail.outbox[0].body
        self.assertIn(self.reviewer_1.full_name, body_1)
        self.assertIn(self.round.name, body_1)

    @override_settings(task_always_eager=True)
    def test_manager_is_notified_on_round_cutoff(self):
        structure_factories.NotificationFactory(
            key="proposal.round_closing_for_managers",
        )
        self.round.cutoff_time = datetime.datetime.now()
        self.round.save()

        tasks.notify_manager_on_round_cutoff()
        self.assertEqual(len(mail.outbox), 1)

        self.assertIn(self.call_manager.email, mail.outbox[0].to)
        self.assertIn(self.call.name, mail.outbox[0].subject)

        body = mail.outbox[0].body
        self.assertIn("Dear call manager", body)
        self.assertIn(self.round.name, body)
        self.assertIn(self.call.name, body)
        self.assertIn(self.round.get_review_strategy_display(), body)
