import datetime

from ddt import data, ddt
from rest_framework import status, test

from waldur_mastermind.proposal import models
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
        "owner",
        "customer_support",
    )
    def test_round_should_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.json()))

    @data("user")
    def test_round_should_not_be_visible(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@ddt
class RoundCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.RoundFactory.get_list_url(self.fixture.call)

    @data(
        "staff",
        "owner",
        "customer_support",
    )
    def test_user_can_add_round_to_call(self, user):
        response = self.create_round(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            models.Round.objects.filter(uuid=response.data["uuid"]).exists()
        )

    @data("user")
    def test_user_can_not_add_offering_to_call(self, user):
        response = self.create_round(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

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
        "owner",
        "customer_support",
    )
    def test_user_can_update_round(self, user):
        response = self.update_round(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data(
        "user",
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
        "owner",
        "customer_support",
    )
    def test_user_can_delete_round(self, user):
        response = self.delete_round(user)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data("user")
    def test_user_can_not_delete_round(self, user):
        response = self.delete_round(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def delete_round(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        return self.client.delete(self.url)
