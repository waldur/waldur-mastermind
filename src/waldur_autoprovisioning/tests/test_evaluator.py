from unittest.mock import patch

from django.test import TestCase

from waldur_autoprovisioning import models
from waldur_autoprovisioning.tests import factories as autoprovisioning_factories
from waldur_core.core.models import User


def _user(**kwargs):
    return User.objects.create(
        username=kwargs.pop("username", "u"),
        email=kwargs.pop("email", "u@example.com"),
        **kwargs,
    )


class EvaluateForUserTest(TestCase):
    """Unit tests for Rule.evaluate_for_user (extracted structured matcher)."""

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_no_filters_required_true_matches(self, _):
        rule = autoprovisioning_factories.RuleFactory()
        user = _user(email="anyone@anywhere.tld")
        result = models.Rule.evaluate_for_user(rule, user)
        self.assertTrue(result.matched)
        # All six filters reported as not configured
        self.assertEqual(len(result.filter_results), 6)
        for fr in result.filter_results:
            self.assertFalse(fr.configured)

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_email_pattern_match(self, _):
        rule = autoprovisioning_factories.RuleFactory(
            user_email_patterns=[r".+@example\.com"]
        )
        user = _user(email="hit@example.com")
        result = models.Rule.evaluate_for_user(rule, user)
        self.assertTrue(result.matched)
        email_fr = next(
            fr for fr in result.filter_results if fr.name == "email_patterns"
        )
        self.assertTrue(email_fr.configured)
        self.assertTrue(email_fr.matched)

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_email_pattern_miss_when_no_other_filters_blocks(self, _):
        rule = autoprovisioning_factories.RuleFactory(
            user_email_patterns=[r".+@allowed\.com"]
        )
        user = _user(email="miss@other.com")
        result = models.Rule.evaluate_for_user(rule, user)
        self.assertFalse(result.matched)

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_affiliations_intersection_match(self, _):
        rule = autoprovisioning_factories.RuleFactory(
            user_affiliations=["staff@uni.tld", "member@uni.tld"]
        )
        user = _user(affiliations=["member@uni.tld"])
        result = models.Rule.evaluate_for_user(rule, user)
        self.assertTrue(result.matched)

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_affiliations_no_intersection_blocks(self, _):
        rule = autoprovisioning_factories.RuleFactory(
            user_affiliations=["staff@uni.tld"]
        )
        user = _user(affiliations=["member@other.tld"])
        result = models.Rule.evaluate_for_user(rule, user)
        self.assertFalse(result.matched)

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_identity_sources_match(self, _):
        rule = autoprovisioning_factories.RuleFactory(
            user_identity_sources=["keycloak"]
        )
        user = _user(identity_source="keycloak")
        result = models.Rule.evaluate_for_user(rule, user)
        self.assertTrue(result.matched)

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_basic_or_logic_passes_if_any_filter_matches(self, _):
        # Email mismatches but affiliation matches — OR group should pass.
        rule = autoprovisioning_factories.RuleFactory(
            user_email_patterns=[r".+@allowed\.com"],
            user_affiliations=["member@uni.tld"],
        )
        user = _user(email="miss@other.com", affiliations=["member@uni.tld"])
        result = models.Rule.evaluate_for_user(rule, user)
        self.assertTrue(result.matched)

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_aai_nationality_filter_blocks(self, _):
        rule = autoprovisioning_factories.RuleFactory(
            user_email_patterns=[r".+@example\.com"],
            user_nationalities=["EE"],
        )
        user = _user(email="hit@example.com", nationality="DE")
        result = models.Rule.evaluate_for_user(rule, user)
        self.assertFalse(result.matched)

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_aai_assurance_level_requires_all(self, _):
        rule = autoprovisioning_factories.RuleFactory(
            user_email_patterns=[r".+@example\.com"],
            user_assurance_levels=["high", "medium"],
        )
        user_partial = _user(
            email="hit@example.com",
            username="partial",
            eduperson_assurance=["high"],
        )
        result = models.Rule.evaluate_for_user(rule, user_partial)
        self.assertFalse(result.matched)

        user_full = _user(
            email="hit@example.com",
            username="full",
            eduperson_assurance=["high", "medium", "extra"],
        )
        result = models.Rule.evaluate_for_user(rule, user_full)
        self.assertTrue(result.matched)

    @patch("waldur_autoprovisioning.handlers.process_order_on_commit")
    def test_get_objects_by_user_patterns_regression(self, _):
        """Refactored matcher returns the same rules as before."""
        matching = autoprovisioning_factories.RuleFactory(
            user_email_patterns=[r".+@example\.com"]
        )
        autoprovisioning_factories.RuleFactory(user_email_patterns=[r".+@other\.com"])
        user = _user(email="hit@example.com")
        matches = models.Rule.get_objects_by_user_patterns(user)
        self.assertEqual([r.pk for r in matches], [matching.pk])
