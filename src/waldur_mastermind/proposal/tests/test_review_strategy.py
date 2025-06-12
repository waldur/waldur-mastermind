from rest_framework import test

from waldur_mastermind.proposal import models, tasks
from waldur_mastermind.proposal.enums import ProposalStates
from waldur_mastermind.proposal.tests import factories, fixtures


class AfterRoundTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.ProposalFactory.get_list_url()
        self.round = self.fixture.round
        self.proposal = factories.ProposalFactory(
            round=self.round, state=ProposalStates.SUBMITTED
        )
        self.proposal_draft = self.fixture.proposal

    def test_create_reviews(self):
        self.assertEqual(self.proposal.review_set.count(), 0)
        self.assertEqual(self.proposal_draft.review_set.count(), 0)

        # create round.minimum_number_of_reviewers reviews
        tasks.create_reviews_if_strategy_is_after_round()
        self.proposal.refresh_from_db()
        self.proposal_draft.refresh_from_db()
        self.assertEqual(self.proposal_draft.state, ProposalStates.CANCELED)
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(
            self.proposal.review_set.filter(state=models.Review.States.CREATED).count(),
            1,
        )
        self.assertEqual(
            self.proposal_draft.review_set.filter().count(),
            0,
        )

        # one review has been rejected
        review = self.proposal.review_set.filter(
            state=models.Review.States.CREATED
        ).get()
        review.state = models.Review.States.REJECTED
        review.save()

        # create another review
        tasks.create_reviews_if_strategy_is_after_round()
        self.proposal.refresh_from_db()
        self.assertEqual(
            self.proposal.review_set.filter(state=models.Review.States.CREATED).count(),
            1,
        )


class AfterProposalTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.url = factories.ProposalFactory.get_list_url()
        self.round = self.fixture.round
        self.round.review_strategy = models.Round.ReviewStrategies.AFTER_PROPOSAL
        self.round.save()
        self.proposal = factories.ProposalFactory(
            round=self.round, state=ProposalStates.SUBMITTED
        )
        self.proposal_draft = self.fixture.proposal

    def test_create_reviews(self):
        self.assertEqual(self.proposal.review_set.count(), 0)
        self.assertEqual(self.proposal_draft.review_set.count(), 0)

        # create round.minimum_number_of_reviewers reviews
        tasks.create_reviews_if_strategy_is_after_proposal()
        self.proposal.refresh_from_db()
        self.proposal_draft.refresh_from_db()
        self.assertEqual(self.proposal_draft.state, ProposalStates.DRAFT)
        self.assertEqual(self.proposal.state, ProposalStates.IN_REVIEW)
        self.assertEqual(
            self.proposal.review_set.filter(state=models.Review.States.CREATED).count(),
            1,
        )
        self.assertEqual(
            self.proposal_draft.review_set.filter().count(),
            0,
        )

        # one review has been rejected
        review = self.proposal.review_set.filter(
            state=models.Review.States.CREATED
        ).get()
        review.state = models.Review.States.REJECTED
        review.save()

        # create another review
        tasks.create_reviews_if_strategy_is_after_proposal()
        self.proposal.refresh_from_db()
        self.assertEqual(
            self.proposal.review_set.filter(state=models.Review.States.CREATED).count(),
            1,
        )
        self.assertEqual(
            self.proposal_draft.review_set.filter().count(),
            0,
        )

        # test task does not raise an error if there are more reviews than minimum required
        self.assertEqual(self.round.minimum_number_of_reviewers, 1)

        models.Review.objects.create(
            proposal=self.proposal,
            state=models.Review.States.CREATED,
            reviewer=self.round.call.reviewers.first(),
        )
        models.Review.objects.create(
            proposal=self.proposal,
            state=models.Review.States.CREATED,
            reviewer=self.round.call.reviewers.first(),
        )

        # call the task again, but it should not create new reviews
        tasks.create_reviews_if_strategy_is_after_proposal()
        self.proposal.refresh_from_db()
        self.assertEqual(
            self.proposal.review_set.filter(state=models.Review.States.CREATED).count(),
            3,
        )
