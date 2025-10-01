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

        # Check that both reviewers received emails (order doesn't matter)
        sent_to_emails = [email.to[0] for email in mail.outbox]
        self.assertIn(self.reviewer_1.email, sent_to_emails)
        self.assertIn(self.reviewer_2.email, sent_to_emails)
        self.assertIn(self.call.name, mail.outbox[0].subject, mail.outbox[1].subject)

        # Check that the email body contains the correct information
        # Find the email sent to reviewer_1
        reviewer_1_email = next(
            email for email in mail.outbox if self.reviewer_1.email in email.to
        )
        self.assertIn(self.reviewer_1.full_name, reviewer_1_email.body)
        self.assertIn(self.round.name, reviewer_1_email.body)

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


class RoundSlugGenerationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()

    def test_round_slug_generation_with_clean_components(self):
        """Test that Round generates proper slug from clean components."""
        # Create a round with clean slug components
        round_obj = models.Round.objects.create(
            call=self.fixture.call,
            start_time=datetime.date(2024, 3, 15),
            cutoff_time=datetime.date(2024, 3, 25),
        )

        # Expected format: ORG-CALL-YYYYMM
        expected_parts = [
            self.fixture.call.manager.customer.slug.upper(),
            self.fixture.call.slug.upper(),
            "202403",  # March 2024
        ]
        expected_slug = "-".join(expected_parts)

        self.assertEqual(round_obj.slug, expected_slug)

    def test_round_slug_generation_with_problematic_components(self):
        """Test that Round cleans duplicate hyphens from components."""
        # Create customer and call with problematic slugs
        customer = structure_factories.CustomerFactory(
            slug="TEST-ORG-"
        )  # Trailing hyphen
        call_manager = factories.CallManagingOrganisationFactory(customer=customer)
        call = factories.CallFactory(
            slug="-CALL-2025",  # Leading hyphen
            manager=call_manager,
        )

        round_obj = models.Round.objects.create(
            call=call,
            start_time=datetime.date(2024, 9, 1),
            cutoff_time=datetime.date(2024, 9, 10),
        )

        # Should clean to: TEST-ORG-CALL-2025-202409
        self.assertEqual(round_obj.slug, "TEST-ORG-CALL-2025-202409")
        self.assertNotIn("--", round_obj.slug)  # No duplicate hyphens
        self.assertFalse(round_obj.slug.startswith("-"))  # No leading hyphen
        self.assertFalse(round_obj.slug.endswith("-"))  # No trailing hyphen

    def test_round_slug_uses_year_month_format(self):
        """Test that Round slug uses YYYYMM format instead of YYYYMMDD."""
        test_dates = [
            (datetime.date(2024, 1, 15), "202401"),
            (datetime.date(2024, 12, 31), "202412"),
            (datetime.date(2025, 6, 1), "202506"),
        ]

        for test_date, expected_date_part in test_dates:
            with self.subTest(date=test_date):
                round_obj = models.Round.objects.create(
                    call=self.fixture.call,
                    start_time=test_date,
                    cutoff_time=test_date + datetime.timedelta(days=10),
                )

                self.assertTrue(round_obj.slug.endswith(expected_date_part))
                # Ensure it's not using YYYYMMDD format
                full_date = test_date.strftime("%Y%m%d")
                self.assertNotIn(full_date, round_obj.slug)

                # Clean up for next iteration
                round_obj.delete()

    def test_round_slug_uniqueness(self):
        """Test that Round slugs are unique when base slug conflicts."""
        # Create first round
        round1 = models.Round.objects.create(
            call=self.fixture.call,
            start_time=datetime.date(2024, 3, 15),
            cutoff_time=datetime.date(2024, 3, 25),
        )

        # Create second round in same month - should get counter
        round2 = models.Round.objects.create(
            call=self.fixture.call,
            start_time=datetime.date(2024, 3, 20),
            cutoff_time=datetime.date(2024, 3, 30),
        )

        # First round should have base slug
        self.assertFalse(round1.slug.endswith(("-1", "-2", "-3")))

        # Second round should have counter
        self.assertTrue(round2.slug.endswith("-1"))

        # Both should start with same base
        base_slug = round1.slug
        self.assertTrue(round2.slug.startswith(base_slug + "-"))

    def test_round_slug_capitalization(self):
        """Test that Round slugs are capitalized."""
        round_obj = models.Round.objects.create(
            call=self.fixture.call,
            start_time=datetime.date(2024, 6, 15),
            cutoff_time=datetime.date(2024, 6, 25),
        )

        # Slug should be all uppercase
        self.assertEqual(round_obj.slug, round_obj.slug.upper())
        self.assertNotEqual(round_obj.slug, round_obj.slug.lower())


class ProposalSlugGenerationTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()

    def test_proposal_slug_generation_with_round_prefix(self):
        """Test that Proposal generates slug with Round prefix."""
        # Create a fresh round for this test to avoid interference
        fresh_round = models.Round.objects.create(
            call=self.fixture.call,
            start_time=datetime.date(2024, 8, 1),
            cutoff_time=datetime.date(2024, 8, 10),
        )

        proposal = models.Proposal.objects.create(
            name="Test Proposal", round=fresh_round
        )

        # Should start with round slug
        expected_prefix = fresh_round.slug + "-"
        self.assertTrue(proposal.slug.startswith(expected_prefix))

        # Should end with 3-digit counter
        suffix = proposal.slug[len(expected_prefix) :]
        self.assertEqual(len(suffix), 3)
        self.assertTrue(suffix.isdigit())
        self.assertEqual(suffix, "001")

        # Clean up
        proposal.delete()
        fresh_round.delete()

    def test_proposal_slug_three_digit_counters(self):
        """Test that Proposal uses 3-digit zero-padded counters."""
        # Create a fresh round for this test
        fresh_round = models.Round.objects.create(
            call=self.fixture.call,
            start_time=datetime.date(2024, 9, 1),
            cutoff_time=datetime.date(2024, 9, 10),
        )

        proposals = []

        # Test the first few counters
        for i in range(3):
            proposal = models.Proposal.objects.create(
                name=f"Test Proposal {i + 1}", round=fresh_round
            )
            proposals.append(proposal)

            # Check that the counter is zero-padded to 3 digits
            counter = proposal.slug.split("-")[-1]
            self.assertEqual(len(counter), 3)
            self.assertTrue(counter.isdigit())
            expected_counter = f"{i + 1:03d}"  # 001, 002, 003
            self.assertEqual(counter, expected_counter)

        # Clean up
        for proposal in proposals:
            proposal.delete()
        fresh_round.delete()

    def test_proposal_slug_with_problematic_round_slug(self):
        """Test Proposal slug generation when Round slug has hyphens."""
        # Create customer and round with hyphens in slugs
        customer = structure_factories.CustomerFactory(
            slug="TEST--ORG"
        )  # Double hyphen
        call_manager = factories.CallManagingOrganisationFactory(customer=customer)
        call = factories.CallFactory(
            slug="CALL-", manager=call_manager
        )  # Trailing hyphen
        round_obj = models.Round.objects.create(
            call=call,
            start_time=datetime.date(2024, 5, 1),
            cutoff_time=datetime.date(2024, 5, 10),
        )

        proposal = models.Proposal.objects.create(name="Test Proposal", round=round_obj)

        # Should have cleaned round slug as prefix
        self.assertNotIn("--", proposal.slug)
        self.assertTrue(proposal.slug.endswith("-001"))

        # Clean up
        proposal.delete()
        round_obj.delete()

    def test_proposal_slug_uniqueness_within_round(self):
        """Test that Proposal slugs are unique within the same round."""
        # Create a fresh round for this test
        fresh_round = models.Round.objects.create(
            call=self.fixture.call,
            start_time=datetime.date(2024, 10, 1),
            cutoff_time=datetime.date(2024, 10, 10),
        )

        proposals = []

        # Create multiple proposals in same round
        for i in range(5):
            proposal = models.Proposal.objects.create(
                name=f"Test Proposal {i + 1}", round=fresh_round
            )
            proposals.append(proposal)

        # All slugs should be different
        slugs = [p.slug for p in proposals]
        self.assertEqual(len(slugs), len(set(slugs)))  # All unique

        # All should start with same round prefix
        round_prefix = fresh_round.slug + "-"
        for slug in slugs:
            self.assertTrue(slug.startswith(round_prefix))

        # Counters should be sequential
        counters = [slug.split("-")[-1] for slug in slugs]
        expected_counters = ["001", "002", "003", "004", "005"]
        self.assertEqual(counters, expected_counters)

        # Clean up
        for proposal in proposals:
            proposal.delete()
        fresh_round.delete()

    def test_proposal_slug_different_rounds(self):
        """Test that Proposals in different rounds have different prefixes."""
        # Create two fresh rounds for this test
        round1 = models.Round.objects.create(
            call=self.fixture.call,
            start_time=datetime.date(2024, 11, 1),
            cutoff_time=datetime.date(2024, 11, 10),
        )

        round2 = models.Round.objects.create(
            call=self.fixture.call,
            start_time=datetime.date(2024, 12, 1),
            cutoff_time=datetime.date(2024, 12, 10),
        )

        proposal1 = models.Proposal.objects.create(
            name="Proposal in Round 1", round=round1
        )

        proposal2 = models.Proposal.objects.create(
            name="Proposal in Round 2", round=round2
        )

        # Should have different prefixes
        prefix1 = round1.slug + "-"
        prefix2 = round2.slug + "-"

        self.assertTrue(proposal1.slug.startswith(prefix1))
        self.assertTrue(proposal2.slug.startswith(prefix2))
        self.assertNotEqual(prefix1, prefix2)

        # Both should end with -001 (first in their respective rounds)
        self.assertTrue(proposal1.slug.endswith("-001"))
        self.assertTrue(proposal2.slug.endswith("-001"))

        # Clean up
        proposal1.delete()
        proposal2.delete()
        round1.delete()
        round2.delete()

    def test_proposal_slug_capitalization(self):
        """Test that Proposal slugs are capitalized."""
        proposal = models.Proposal.objects.create(
            name="Test Proposal", round=self.fixture.round
        )

        # Slug should be all uppercase
        self.assertEqual(proposal.slug, proposal.slug.upper())
        self.assertNotEqual(proposal.slug, proposal.slug.lower())

        # Clean up
        proposal.delete()


class SlugUtilityTest(test.APITransactionTestCase):
    def test_clean_slug_hyphens_function(self):
        """Test the clean_slug_hyphens utility function."""
        from waldur_core.core.models import clean_slug_hyphens

        test_cases = [
            ("normal-slug", "normal-slug"),  # No change needed
            ("slug--with--doubles", "slug-with-doubles"),  # Remove doubles
            ("slug---with---triples", "slug-with-triples"),  # Remove triples
            ("-leading-hyphen", "leading-hyphen"),  # Remove leading
            ("trailing-hyphen-", "trailing-hyphen"),  # Remove trailing
            ("-both-leading-and-trailing-", "both-leading-and-trailing"),  # Both
            ("----multiple----", "multiple"),  # Extreme case
            ("", ""),  # Empty string
            ("-", ""),  # Only hyphens
            ("--", ""),  # Only double hyphens
        ]

        for input_slug, expected_output in test_cases:
            with self.subTest(input=input_slug):
                result = clean_slug_hyphens(input_slug)
                self.assertEqual(result, expected_output)
