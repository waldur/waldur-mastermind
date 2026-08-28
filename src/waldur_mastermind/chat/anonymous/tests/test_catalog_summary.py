"""Tests for the catalog summary helper used by the anonymous chat system prompt."""

from constance.test import override_config
from django.test import TestCase

from waldur_mastermind.chat.anonymous.catalog import build_catalog_summary
from waldur_mastermind.marketplace.enums import OfferingStates
from waldur_mastermind.marketplace.tests import factories as mp_factories


class CatalogSummaryAnonymousVisibilityTest(TestCase):
    """The summary always uses anon-capped visibility — never widens scope."""

    def setUp(self):
        self.shared_active = mp_factories.OfferingFactory(
            shared=True, state=OfferingStates.ACTIVE, name="LUMI"
        )
        self.private_active = mp_factories.OfferingFactory(
            shared=False, state=OfferingStates.ACTIVE, name="PrivateOffering"
        )
        self.shared_draft = mp_factories.OfferingFactory(
            shared=True, state=OfferingStates.DRAFT, name="DraftOffering"
        )

    @override_config(
        ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True,
        ANONYMOUS_CHAT_CATALOG_MAX_ENTRIES=50,
    )
    def test_includes_shared_active_offerings(self):
        result = build_catalog_summary()
        self.assertIn("LUMI", result)

    @override_config(
        ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True,
        ANONYMOUS_CHAT_CATALOG_MAX_ENTRIES=50,
    )
    def test_excludes_private_offerings(self):
        result = build_catalog_summary()
        self.assertNotIn("PrivateOffering", result)

    @override_config(
        ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True,
        ANONYMOUS_CHAT_CATALOG_MAX_ENTRIES=50,
    )
    def test_excludes_draft_offerings(self):
        result = build_catalog_summary()
        self.assertNotIn("DraftOffering", result)

    @override_config(
        ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False,
        ANONYMOUS_CHAT_CATALOG_MAX_ENTRIES=50,
    )
    def test_returns_empty_marker_when_no_offerings_visible(self):
        result = build_catalog_summary()
        self.assertIn("no public offerings", result.lower())


class CatalogSummaryCapTest(TestCase):
    """Hard cap and tail-drop behaviour."""

    def setUp(self):
        # Create 8 active+shared offerings — small enough not to slow tests
        # but enough to test cap behaviour with a 5-entry limit.
        self.offerings = [
            mp_factories.OfferingFactory(
                shared=True,
                state=OfferingStates.ACTIVE,
                name=f"Offer-{i:02d}",
            )
            for i in range(8)
        ]

    @override_config(
        ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True,
        ANONYMOUS_CHAT_CATALOG_MAX_ENTRIES=5,
    )
    def test_drops_tail_past_cap(self):
        result = build_catalog_summary()
        # 5 lines + 1 "more not shown" line = 6 lines max
        self.assertEqual(len(result.splitlines()), 6)
        self.assertIn("3 more", result)

    @override_config(
        ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True,
        ANONYMOUS_CHAT_CATALOG_MAX_ENTRIES=20,
    )
    def test_no_tail_marker_when_under_cap(self):
        result = build_catalog_summary()
        self.assertNotIn("more offering(s) not shown", result)


class CatalogSummaryCountryPrecedenceTest(TestCase):
    """Country label must match the tool serializers: offering.country wins."""

    @override_config(
        ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True,
        ANONYMOUS_CHAT_CATALOG_MAX_ENTRIES=50,
    )
    def test_offering_country_wins_over_provider_country(self):
        offering = mp_factories.OfferingFactory(
            shared=True, state=OfferingStates.ACTIVE, name="Hosted-Elsewhere"
        )
        offering.country = "DE"
        offering.save()
        offering.customer.country = "EE"
        offering.customer.save()
        result = build_catalog_summary()
        self.assertIn("(DE)", result)
        self.assertNotIn("(EE)", result)

    @override_config(
        ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True,
        ANONYMOUS_CHAT_CATALOG_MAX_ENTRIES=50,
    )
    def test_description_preview_is_html_stripped(self):
        mp_factories.OfferingFactory(
            shared=True,
            state=OfferingStates.ACTIVE,
            name="Rich-Text",
            description='<p style="text-align: justify;">GPU <strong>cluster</strong></p>',
        )
        result = build_catalog_summary()
        self.assertIn("GPU cluster", result)
        self.assertNotIn("<p", result)


class CatalogSummaryFormatTest(TestCase):
    """Per-line formatting invariants — guards against renderer regressions."""

    @override_config(
        ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True,
        ANONYMOUS_CHAT_CATALOG_MAX_ENTRIES=50,
    )
    def test_long_description_is_truncated(self):
        long_desc = "x" * 500
        mp_factories.OfferingFactory(
            shared=True,
            state=OfferingStates.ACTIVE,
            name="LongDesc",
            description=long_desc,
        )
        result = build_catalog_summary()
        self.assertIn("LongDesc", result)
        self.assertIn("…", result)
        # Truncation kicks in at 120 chars — no 500-char run survives
        self.assertNotIn("x" * 200, result)

    @override_config(
        ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True,
        ANONYMOUS_CHAT_CATALOG_MAX_ENTRIES=50,
    )
    def test_pipe_in_name_is_escaped(self):
        # Names with the literal | character would corrupt the
        # one-line-per-offering format the LLM keys off.
        mp_factories.OfferingFactory(
            shared=True,
            state=OfferingStates.ACTIVE,
            name="Has|Pipe",
        )
        result = build_catalog_summary()
        self.assertNotIn("Has|Pipe", result)
        self.assertIn("Has/Pipe", result)
