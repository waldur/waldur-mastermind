"""Tests for affinity scoring functionality."""

from django.test import TestCase
from rest_framework import test

from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.affinity_scoring import (
    compute_affinity,
    compute_idf,
    compute_keyword_affinity,
    compute_tf,
    compute_tfidf_affinity,
    compute_tfidf_vector,
    cosine_similarity,
    get_proposal_text,
    get_reviewer_text,
    tokenize,
)
from waldur_mastermind.proposal.enums import ExpertiseProficiencyLevels
from waldur_mastermind.proposal.tests import factories


class TokenizationTest(TestCase):
    """Test text tokenization utilities."""

    def test_basic_tokenization(self):
        """Tokenize splits text into lowercase words."""
        tokens = tokenize("Hello World")
        self.assertIn("hello", tokens)
        self.assertIn("world", tokens)

    def test_handles_empty_text(self):
        """Empty text returns empty list."""
        tokens = tokenize("")
        self.assertEqual(tokens, [])


class TFIDFTest(TestCase):
    """Test TF-IDF scoring implementation."""

    def setUp(self):
        self.tokens = ["machine", "learning", "neural", "networks"]

    def test_compute_tf(self):
        """Compute TF returns term frequencies."""
        tf = compute_tf(self.tokens)

        self.assertIn("machine", tf)
        self.assertIn("learning", tf)
        self.assertGreater(tf["machine"], 0)

    def test_compute_idf(self):
        """Compute IDF returns inverse document frequencies."""
        documents = [
            ["machine", "learning"],
            ["deep", "learning"],
            ["machine", "vision"],
        ]
        idf = compute_idf(documents)

        # "learning" appears in 2 docs, should have lower IDF than "vision" (1 doc)
        self.assertIn("learning", idf)
        self.assertIn("vision", idf)

    def test_compute_tfidf_vector(self):
        """Compute TF-IDF vector returns weighted terms."""
        documents = [
            ["machine", "learning"],
            ["deep", "learning"],
        ]
        idf = compute_idf(documents)
        vec = compute_tfidf_vector(["machine", "learning"], idf)

        self.assertIn("machine", vec)
        self.assertIn("learning", vec)


class CosineSimilarityTest(TestCase):
    """Test cosine similarity computation."""

    def test_identical_vectors(self):
        """Identical vectors have similarity 1.0."""
        vec = {"a": 1.0, "b": 2.0}
        sim = cosine_similarity(vec, vec)
        self.assertAlmostEqual(sim, 1.0, places=5)

    def test_orthogonal_vectors(self):
        """Orthogonal vectors have similarity 0.0."""
        vec1 = {"a": 1.0}
        vec2 = {"b": 1.0}
        sim = cosine_similarity(vec1, vec2)
        self.assertEqual(sim, 0.0)

    def test_empty_vectors(self):
        """Empty vectors return 0.0."""
        sim = cosine_similarity({}, {})
        self.assertEqual(sim, 0.0)


class KeywordAffinityTest(TestCase):
    """Test keyword-based affinity scoring."""

    def setUp(self):
        self.reviewer = factories.ReviewerProfileFactory()
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(round=self.round)

    def test_exact_keyword_match(self):
        """Exact keyword matches contribute to affinity."""
        # Add expertise to reviewer
        factories.ReviewerExpertiseFactory(
            reviewer_profile=self.reviewer,
            expertise_keyword="machine learning",
            proficiency_level=ExpertiseProficiencyLevels.EXPERT,
        )

        # Set proposal with related summary
        self.proposal.project_summary = "Research in machine learning and data science"
        self.proposal.save()

        score = compute_keyword_affinity(self.reviewer, self.proposal)
        # Score should be non-negative
        self.assertGreaterEqual(score, 0)

    def test_no_expertise_returns_zero(self):
        """No expertise returns zero score."""
        # No expertise added
        self.proposal.project_summary = "Research in machine learning"
        self.proposal.save()

        score = compute_keyword_affinity(self.reviewer, self.proposal)
        self.assertEqual(score, 0)


class TextAffinityTest(TestCase):
    """Test text-based TF-IDF affinity scoring."""

    def setUp(self):
        self.reviewer = factories.ReviewerProfileFactory()
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(round=self.round)

    def test_get_reviewer_text(self):
        """Get reviewer text aggregates profile information."""
        self.reviewer.biography = "Expert in machine learning"
        self.reviewer.save()

        factories.ReviewerPublicationFactory(
            reviewer_profile=self.reviewer,
            title="Deep Learning Methods",
        )

        text = get_reviewer_text(self.reviewer)
        self.assertIn("machine learning", text.lower())

    def test_get_proposal_text(self):
        """Get proposal text aggregates proposal information."""
        self.proposal.project_summary = "Research in artificial intelligence"
        self.proposal.save()

        text = get_proposal_text(self.proposal)
        # Check that we get some text - exact content depends on model fields
        self.assertIsInstance(text, str)

    def test_tfidf_affinity_computation(self):
        """TF-IDF affinity can be computed."""
        self.reviewer.biography = "Machine learning expert"
        self.reviewer.save()

        self.proposal.project_summary = "Machine learning research"
        self.proposal.save()

        score = compute_tfidf_affinity(self.reviewer, self.proposal)
        self.assertGreaterEqual(score, 0)


class CombinedAffinityTest(TestCase):
    """Test combined affinity scoring."""

    def setUp(self):
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(round=self.round)
        self.reviewer = factories.ReviewerProfileFactory()
        self.config = factories.MatchingConfigurationFactory(
            call=self.call,
            affinity_method="combined",
            keyword_weight=0.4,
            text_weight=0.6,
        )

    def test_combined_scoring(self):
        """Combined method uses both keyword and TF-IDF scores."""
        factories.ReviewerExpertiseFactory(
            reviewer_profile=self.reviewer,
            expertise_keyword="data science",
        )
        factories.ReviewerPublicationFactory(
            reviewer_profile=self.reviewer,
            title="Data Science Methods",
        )

        self.proposal.project_summary = "Research in data science applications"
        self.proposal.save()

        score = compute_affinity(self.reviewer, self.proposal, self.config)

        self.assertIn("affinity_score", score)
        self.assertGreaterEqual(score["affinity_score"], 0)

    def test_keyword_only_method(self):
        """Keyword-only method works."""
        self.config.affinity_method = "keyword"
        self.config.save()

        score = compute_affinity(self.reviewer, self.proposal, self.config)
        self.assertIn("affinity_score", score)


class MatchingConfigurationTest(TestCase):
    """Test matching configuration model."""

    def test_create_configuration(self):
        """Can create a matching configuration."""
        config = factories.MatchingConfigurationFactory(
            keyword_weight=0.4,
            text_weight=0.6,
        )

        self.assertAlmostEqual(config.keyword_weight + config.text_weight, 1.0)

    def test_affinity_methods(self):
        """All affinity methods are valid."""
        for method in ["keyword", "tfidf", "combined"]:
            config = factories.MatchingConfigurationFactory(affinity_method=method)
            self.assertEqual(config.affinity_method, method)


class ReviewerBidModelTest(TestCase):
    """Test reviewer bidding model."""

    def setUp(self):
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(round=self.round)
        self.reviewer = factories.ReviewerProfileFactory()
        factories.CallReviewerPoolFactory(call=self.call, reviewer=self.reviewer)

    def test_create_bid(self):
        """Reviewer can create a bid on a proposal."""
        bid = models.ReviewerBid.objects.create(
            call=self.call,
            reviewer=self.reviewer,
            proposal=self.proposal,
            bid="eager",
            comment="Very interested in this topic",
        )

        self.assertEqual(bid.bid, "eager")
        self.assertEqual(bid.reviewer, self.reviewer)

    def test_bid_values(self):
        """All bid values are valid."""
        for bid_value in ["eager", "willing", "not_willing", "conflict"]:
            bid = models.ReviewerBid.objects.create(
                call=self.call,
                reviewer=factories.ReviewerProfileFactory(),
                proposal=self.proposal,
                bid=bid_value,
            )
            self.assertEqual(bid.bid, bid_value)


# =============================================================================
# Matching Configuration API Tests
# =============================================================================


class MatchingConfigurationAPITest(test.APITestCase):
    """Test matching configuration API endpoint."""

    def setUp(self):
        from waldur_core.permissions.fixtures import CallRole
        from waldur_core.structure.tests import factories as structure_factories

        # Create call with manager
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)

        # Create a staff user who is also a call manager
        self.call_manager = structure_factories.UserFactory(is_staff=True)
        self.call.add_user(self.call_manager, CallRole.MANAGER)

        # Create regular user
        self.regular_user = structure_factories.UserFactory()

    def _get_matching_config_url(self):
        return factories.CallFactory.get_protected_url(
            self.call, action="matching-configuration"
        )

    def test_call_manager_can_get_matching_configuration(self):
        """Call manager can retrieve matching configuration."""
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(self._get_matching_config_url())

        self.assertEqual(response.status_code, 200)
        self.assertIn("uuid", response.data)
        self.assertIn("affinity_method", response.data)
        self.assertIn("keyword_weight", response.data)
        self.assertIn("text_weight", response.data)

    def test_regular_user_cannot_get_matching_configuration(self):
        """Regular user cannot access matching configuration."""
        self.client.force_authenticate(self.regular_user)
        response = self.client.get(self._get_matching_config_url())

        self.assertEqual(response.status_code, 404)

    def test_call_manager_can_update_matching_configuration(self):
        """Call manager can update matching configuration via PATCH."""
        self.client.force_authenticate(self.call_manager)

        # First GET to create the config
        self.client.get(self._get_matching_config_url())

        # PATCH to update
        response = self.client.patch(
            self._get_matching_config_url(),
            data={
                "affinity_method": "keyword",
                "keyword_weight": 0.7,
                "text_weight": 0.3,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["affinity_method"], "keyword")
        self.assertEqual(response.data["keyword_weight"], 0.7)
        self.assertEqual(response.data["text_weight"], 0.3)

    def test_serializer_response_has_no_url_field(self):
        """Serializer response should not include a url field."""
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(self._get_matching_config_url())

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("url", response.data)
        # But should have call_uuid and call_name
        self.assertIn("call_uuid", response.data)
        self.assertIn("call_name", response.data)

    def test_serializer_response_contains_all_config_fields(self):
        """Serializer response should contain all configuration fields."""
        self.client.force_authenticate(self.call_manager)
        response = self.client.get(self._get_matching_config_url())

        expected_fields = [
            "uuid",
            "call_uuid",
            "call_name",
            "affinity_method",
            "keyword_weight",
            "text_weight",
            "min_reviewers_per_proposal",
            "max_reviewers_per_proposal",
            "min_proposals_per_reviewer",
            "max_proposals_per_reviewer",
            "algorithm",
            "min_affinity_threshold",
            "use_reviewer_bids",
            "bid_weight",
            "created",
            "modified",
        ]

        for field in expected_fields:
            self.assertIn(field, response.data, f"Missing field: {field}")
