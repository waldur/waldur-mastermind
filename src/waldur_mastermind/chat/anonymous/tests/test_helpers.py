"""Tests for the anonymous-chat helpers (slug + feedback token)."""

from unittest import mock

import pytest
from constance.test import override_config
from django.db import DatabaseError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.anonymous import helpers
from waldur_mastermind.chat.anonymous.helpers import (
    compute_feedback_token,
    compute_user_slug,
    verify_feedback_token,
)
from waldur_mastermind.marketplace.enums import OfferingStates
from waldur_mastermind.marketplace.tests import factories as mp_factories


class UserSlugTest(TestCase):
    @override_config(ANONYMOUS_CHAT_USER_SLUG_SALT="test-salt-please-rotate")
    def test_deterministic_for_same_ip(self):
        a = compute_user_slug("1.2.3.4")
        b = compute_user_slug("1.2.3.4")
        self.assertEqual(a, b)
        self.assertTrue(a)

    @override_config(ANONYMOUS_CHAT_USER_SLUG_SALT="test-salt-please-rotate")
    def test_different_for_different_ips(self):
        a = compute_user_slug("1.2.3.4")
        b = compute_user_slug("9.9.9.9")
        self.assertNotEqual(a, b)

    @override_config(ANONYMOUS_CHAT_USER_SLUG_SALT="salt-A")
    def test_different_when_salt_rotates(self):
        a = compute_user_slug("1.2.3.4")
        with override_config(ANONYMOUS_CHAT_USER_SLUG_SALT="salt-B"):
            b = compute_user_slug("1.2.3.4")
        self.assertNotEqual(a, b)

    @override_config(ANONYMOUS_CHAT_USER_SLUG_SALT="")
    def test_returns_empty_when_salt_unset(self):
        # No salt → no slug. Operator must initialise it.
        self.assertEqual(compute_user_slug("1.2.3.4"), "")

    @override_config(ANONYMOUS_CHAT_USER_SLUG_SALT="test-salt")
    def test_returns_empty_for_empty_ip(self):
        self.assertEqual(compute_user_slug(""), "")
        self.assertEqual(compute_user_slug(None), "")


@override_config(ANONYMOUS_CHAT_FEEDBACK_TOKEN_SECRET="test-secret-please-rotate")
class FeedbackTokenTest(TestCase):
    def test_deterministic_for_same_inputs(self):
        a = compute_feedback_token("uuid-1", "session-x", "1.2.3.4")
        b = compute_feedback_token("uuid-1", "session-x", "1.2.3.4")
        self.assertEqual(a, b)
        self.assertTrue(a)

    def test_different_for_different_uuid(self):
        a = compute_feedback_token("uuid-1", "session-x", "1.2.3.4")
        b = compute_feedback_token("uuid-2", "session-x", "1.2.3.4")
        self.assertNotEqual(a, b)

    def test_different_for_different_session(self):
        a = compute_feedback_token("uuid-1", "session-x", "1.2.3.4")
        b = compute_feedback_token("uuid-1", "session-y", "1.2.3.4")
        self.assertNotEqual(a, b)

    def test_different_for_different_ip(self):
        # The IP-binding is the whole point — spoofing IP shouldn't
        # let an attacker forge tokens for a victim's interaction.
        a = compute_feedback_token("uuid-1", "session-x", "1.2.3.4")
        b = compute_feedback_token("uuid-1", "session-x", "9.9.9.9")
        self.assertNotEqual(a, b)

    def test_verify_accepts_correct_token(self):
        token = compute_feedback_token("uuid-1", "session-x", "1.2.3.4")
        self.assertTrue(verify_feedback_token(token, "uuid-1", "session-x", "1.2.3.4"))

    def test_verify_rejects_wrong_token(self):
        self.assertFalse(
            verify_feedback_token("not-the-token", "uuid-1", "session-x", "1.2.3.4")
        )

    def test_verify_rejects_when_ip_changed(self):
        # An attacker gets the uuid + session + token from a victim's
        # response but tries to submit feedback from their own IP — the
        # token won't verify.
        token = compute_feedback_token("uuid-1", "session-x", "1.2.3.4")
        self.assertFalse(verify_feedback_token(token, "uuid-1", "session-x", "9.9.9.9"))


class FeedbackTokenFailSafeTest(TestCase):
    @override_config(ANONYMOUS_CHAT_FEEDBACK_TOKEN_SECRET="")
    def test_compute_returns_empty_when_secret_unset(self):
        # No secret → no token. Operator must initialise it; until then,
        # the feedback endpoint returns 403 for everyone (which is the
        # safe default — better closed than predictable).
        self.assertEqual(compute_feedback_token("uuid-1", "session-x", "1.2.3.4"), "")

    @override_config(ANONYMOUS_CHAT_FEEDBACK_TOKEN_SECRET="")
    def test_verify_returns_false_when_secret_unset(self):
        self.assertFalse(
            verify_feedback_token("any-string", "uuid-1", "session-x", "1.2.3.4")
        )

    @override_config(ANONYMOUS_CHAT_FEEDBACK_TOKEN_SECRET="test-secret")
    def test_compute_returns_empty_for_missing_inputs(self):
        self.assertEqual(compute_feedback_token("", "session-x", "1.2.3.4"), "")
        self.assertEqual(compute_feedback_token("uuid-1", "", "1.2.3.4"), "")
        self.assertEqual(compute_feedback_token("uuid-1", "session-x", ""), "")

    @override_config(ANONYMOUS_CHAT_FEEDBACK_TOKEN_SECRET="test-secret")
    def test_verify_returns_false_for_empty_token(self):
        self.assertFalse(verify_feedback_token("", "uuid-1", "session-x", "1.2.3.4"))


@pytest.mark.django_db
@override_config(SITE_DESCRIPTION="Estonian research compute services")
def test_build_domain_context_uses_site_description_and_visible_categories():
    cat_visible = mp_factories.CategoryFactory(title="Compute")
    mp_factories.CategoryFactory(title="Storage")  # no offerings → excluded
    mp_factories.OfferingFactory(
        category=cat_visible, shared=True, state=OfferingStates.PAUSED
    )

    ctx = helpers.build_domain_context()

    assert "Estonian research compute services" in ctx
    assert "Compute" in ctx
    assert "Storage" not in ctx


@pytest.mark.django_db
@override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
def test_offering_format_hint_omits_country_when_single_country():
    cat = mp_factories.CategoryFactory(title="Compute")
    cust = structure_factories.CustomerFactory(country="EE")
    mp_factories.OfferingFactory(
        category=cat, customer=cust, shared=True, state=OfferingStates.ACTIVE
    )
    mp_factories.OfferingFactory(
        category=cat, customer=cust, shared=True, state=OfferingStates.ACTIVE
    )

    hint = helpers.build_offering_format_hint()
    assert "(Provider)" in hint
    assert "Country" not in hint


@pytest.mark.django_db
@override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
def test_offering_format_hint_includes_country_when_multi_country():
    cat = mp_factories.CategoryFactory(title="Compute")
    for cc in ("EE", "FI", "DE"):
        cust = structure_factories.CustomerFactory(country=cc)
        mp_factories.OfferingFactory(
            category=cat, customer=cust, shared=True, state=OfferingStates.ACTIVE
        )

    hint = helpers.build_offering_format_hint()
    assert "(Provider, Country)" in hint


@pytest.mark.django_db
@override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
def test_offering_format_hint_counts_offering_level_countries():
    # Providers share one registration country, but the offerings themselves
    # are hosted in different countries — the hint must still include Country.
    cat = mp_factories.CategoryFactory(title="Compute")
    cust = structure_factories.CustomerFactory(country="EE")
    for cc in ("DE", "FI"):
        mp_factories.OfferingFactory(
            category=cat,
            customer=cust,
            country=cc,
            shared=True,
            state=OfferingStates.ACTIVE,
        )

    hint = helpers.build_offering_format_hint()
    assert "(Provider, Country)" in hint


@pytest.mark.django_db
def test_offering_format_hint_defaults_to_no_country_when_catalog_empty():
    # Anonymous can't see anything → without-country (smaller prompt).
    hint = helpers.build_offering_format_hint()
    assert "(Provider)" in hint
    assert "Country" not in hint


@pytest.mark.django_db
@override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
def test_offering_format_hint_country_query_selects_countries_only():
    # Offering.Meta.ordering injects name/id into SELECT DISTINCT; id is
    # unique, so that DISTINCT collapses nothing and the query yields one
    # row per visible offering instead of one per country pair.
    cat = mp_factories.CategoryFactory(title="Compute")
    cust = structure_factories.CustomerFactory(country="EE")
    for _ in range(3):
        mp_factories.OfferingFactory(
            category=cat, customer=cust, shared=True, state=OfferingStates.ACTIVE
        )

    with CaptureQueriesContext(connection) as ctx:
        hint = helpers.build_offering_format_hint()

    assert "(Provider)" in hint
    distinct_sql = next(
        q["sql"] for q in ctx.captured_queries if "DISTINCT" in q["sql"]
    )
    select_clause = distinct_sql.split(" FROM ")[0]
    assert '"marketplace_offering"."id"' not in select_clause
    assert '"marketplace_offering"."name"' not in select_clause


def test_offering_format_hint_degrades_on_db_error():
    with mock.patch.object(
        helpers, "offerings_queryset_for", side_effect=DatabaseError
    ):
        hint = helpers.build_offering_format_hint()
    assert "(Provider)" in hint
    assert "Country" not in hint


def test_offering_format_hint_propagates_non_db_errors():
    # Only DB unavailability (schema generation, migrations) may degrade the
    # prompt silently — a programming error must surface.
    with mock.patch.object(helpers, "offerings_queryset_for", side_effect=ValueError):
        with pytest.raises(ValueError):
            helpers.build_offering_format_hint()
