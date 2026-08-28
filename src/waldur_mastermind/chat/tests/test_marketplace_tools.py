import time

from constance.test import override_config
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.tools.enums import ToolName
from waldur_mastermind.chat.tools.marketplace.compare_offerings import (
    CompareOfferingsTool,
)
from waldur_mastermind.chat.tools.marketplace.get_offering import GetOfferingTool
from waldur_mastermind.chat.tools.marketplace.helpers import (
    cap_text,
    is_public_marketplace_enabled,
    offering_homeport_url,
    offerings_queryset_for,
    serialize_offering_detailed,
    serialize_offering_minimal,
    strip_html_to_text,
)
from waldur_mastermind.chat.tools.marketplace.list_categories import ListCategoriesTool
from waldur_mastermind.chat.tools.marketplace.search_offerings import (
    SearchOfferingsTool,
)
from waldur_mastermind.chat.tools.registry import tool_registry
from waldur_mastermind.marketplace.enums import OfferingStates
from waldur_mastermind.marketplace.tests import factories as mp_factories
from waldur_mastermind.marketplace.tests import fixtures as mp_fixtures


class MarketplaceToolEnumTest(TestCase):
    def test_search_offerings_enum(self):
        self.assertEqual(ToolName.SEARCH_OFFERINGS.value, "search_offerings")

    def test_get_offering_enum(self):
        self.assertEqual(ToolName.GET_OFFERING.value, "get_offering")

    def test_list_categories_enum(self):
        self.assertEqual(ToolName.LIST_CATEGORIES.value, "list_categories")

    def test_compare_offerings_enum(self):
        self.assertEqual(ToolName.COMPARE_OFFERINGS.value, "compare_offerings")


class PublicMarketplaceFlagTest(TestCase):
    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_enabled_when_flag_true(self):
        self.assertTrue(is_public_marketplace_enabled())

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_disabled_when_flag_false(self):
        self.assertFalse(is_public_marketplace_enabled())


class OfferingsQuerysetForAnonymousTest(TestCase):
    """Anonymous (or None) caller path — must stay locked to the public surface."""

    def setUp(self):
        self.shared_active = mp_factories.OfferingFactory(
            shared=True, state=OfferingStates.ACTIVE
        )
        self.shared_paused = mp_factories.OfferingFactory(
            shared=True, state=OfferingStates.PAUSED
        )
        self.shared_draft = mp_factories.OfferingFactory(
            shared=True, state=OfferingStates.DRAFT
        )
        self.private_active = mp_factories.OfferingFactory(
            shared=False, state=OfferingStates.ACTIVE
        )

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_returns_shared_active_and_paused(self):
        uuids = set(offerings_queryset_for().values_list("uuid", flat=True))
        self.assertEqual(uuids, {self.shared_active.uuid, self.shared_paused.uuid})

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_excludes_private_offerings(self):
        self.assertNotIn(self.private_active, offerings_queryset_for())

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_excludes_draft_offerings(self):
        self.assertNotIn(self.shared_draft, offerings_queryset_for())

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_empty_when_flag_disabled(self):
        self.assertEqual(offerings_queryset_for().count(), 0)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_explicit_anonymous_user_matches_none(self):
        none_uuids = set(offerings_queryset_for(None).values_list("uuid", flat=True))
        anon_uuids = set(
            offerings_queryset_for(AnonymousUser()).values_list("uuid", flat=True)
        )
        self.assertEqual(none_uuids, anon_uuids)


class MarketplaceToolFlagBypassForAuthenticatedTest(TestCase):
    """Authenticated callers must bypass the ANONYMOUS_USER_CAN_VIEW_OFFERINGS gate."""

    def setUp(self):
        self.fixture = mp_fixtures.MarketplaceFixture()
        self.user = self.fixture.staff
        mp_factories.OfferingFactory(
            name="GPU", shared=True, state=OfferingStates.ACTIVE
        )

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_search_offerings_succeeds_when_flag_disabled(self):
        result = SearchOfferingsTool().execute(self.user, {"keyword": "GPU"})
        self.assertEqual(result["type"], "success")

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_get_offering_succeeds_when_flag_disabled(self):
        offering = mp_factories.OfferingFactory(
            name="X", shared=True, state=OfferingStates.ACTIVE
        )
        result = GetOfferingTool().execute(self.user, {"uuid": str(offering.uuid)})
        self.assertEqual(result["type"], "success")

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_list_categories_succeeds_when_flag_disabled(self):
        result = ListCategoriesTool().execute(self.user, {})
        self.assertEqual(result["type"], "success")

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_compare_offerings_succeeds_when_flag_disabled(self):
        a = mp_factories.OfferingFactory(
            name="A", shared=True, state=OfferingStates.ACTIVE
        )
        b = mp_factories.OfferingFactory(
            name="B", shared=True, state=OfferingStates.ACTIVE
        )
        result = CompareOfferingsTool().execute(
            self.user, {"uuids": [str(a.uuid), str(b.uuid)]}
        )
        self.assertEqual(result["type"], "success")


class OfferingsQuerysetForAuthenticatedTest(TestCase):
    """Authenticated callers see what marketplace.filter_by_ordering_availability_for_user
    returns for them — staff/support see shared offerings PLUS offerings that
    have at least one non-archived plan; regular users see the public surface
    plus their org-group-restricted offerings.
    """

    def setUp(self):
        self.shared_active = mp_factories.OfferingFactory(
            shared=True, state=OfferingStates.ACTIVE
        )
        self.shared_draft = mp_factories.OfferingFactory(
            shared=True, state=OfferingStates.DRAFT
        )
        # Private offering without a plan: invisible even to staff.
        self.private_no_plan = mp_factories.OfferingFactory(
            shared=False, state=OfferingStates.ACTIVE
        )
        # Private offering WITH a non-archived plan: visible to staff/support.
        self.private_with_plan = mp_factories.OfferingFactory(
            shared=False, state=OfferingStates.ACTIVE
        )
        mp_factories.PlanFactory(offering=self.private_with_plan, archived=False)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_staff_sees_private_offerings_with_plans(self):
        # Staff see public offerings AND private offerings that have plans —
        # this is the broader visibility that distinguishes staff from anon.
        staff = structure_factories.UserFactory(is_staff=True)
        uuids = set(offerings_queryset_for(staff).values_list("uuid", flat=True))
        self.assertIn(self.shared_active.uuid, uuids)
        self.assertIn(self.private_with_plan.uuid, uuids)
        # Anonymous would NOT see this private one — confirms the user-aware
        # path is actually doing more than the anon path.
        anon_uuids = set(offerings_queryset_for().values_list("uuid", flat=True))
        self.assertNotIn(self.private_with_plan.uuid, anon_uuids)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_staff_unaffected_by_anon_flag(self):
        # The anon flag gates anonymous viewers only — staff still see their
        # full scope (shared + plan-bearing offerings) regardless.
        staff = structure_factories.UserFactory(is_staff=True)
        uuids = set(offerings_queryset_for(staff).values_list("uuid", flat=True))
        self.assertIn(self.shared_active.uuid, uuids)
        self.assertIn(self.private_with_plan.uuid, uuids)
        # And anon, with the flag off, sees nothing.
        self.assertEqual(offerings_queryset_for().count(), 0)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_regular_user_sees_at_least_shared_active(self):
        # Regular user without org-group membership sees the public surface
        # at minimum (same as anon's lower bound) — they may also see more
        # via project/org-group connections, but never less.
        user = structure_factories.UserFactory()
        uuids = set(offerings_queryset_for(user).values_list("uuid", flat=True))
        self.assertIn(self.shared_active.uuid, uuids)
        self.assertNotIn(self.shared_draft.uuid, uuids)


class OfferingHomeportUrlTest(TestCase):
    @override_config(HOMEPORT_URL="https://hub.example.org/")
    def test_strips_trailing_slash_and_builds_path(self):
        url = offering_homeport_url("abc-uuid")
        self.assertEqual(
            url, "https://hub.example.org/marketplace-public-offering/abc-uuid/"
        )


class SerializeOfferingMinimalTest(TestCase):
    @override_config(HOMEPORT_URL="https://hub.example.org")
    def test_returns_all_expected_keys(self):
        offering = mp_factories.OfferingFactory(
            name="GPU Cluster",
            description="High-end GPU for AI workloads.",
            type="Marketplace.Slurm",
        )
        result = serialize_offering_minimal(offering)
        # Plugin-level `type` (e.g. Marketplace.Slurm) and the API-level
        # `url` are deliberately NOT exposed — both leak into LLM
        # narration as noise. `homeport_url` IS exposed: the LLM drops it
        # straight into a markdown `[Open](url)` cell, never paraphrasing.
        self.assertEqual(
            set(result.keys()),
            {
                "name",
                "uuid",
                "category_title",
                "customer_name",
                "country",
                "description",
                "starting_price",
                "has_access_url",
                "homeport_url",
            },
        )
        self.assertEqual(result["name"], "GPU Cluster")
        self.assertNotIn("type", result)
        self.assertNotIn("url", result)
        self.assertTrue(result["homeport_url"].startswith("https://hub.example.org/"))

    def test_description_truncated_to_500_chars_with_marker(self):
        offering = mp_factories.OfferingFactory(description="x" * 1000)
        result = serialize_offering_minimal(offering)
        self.assertEqual(len(result["description"]), 500)
        self.assertTrue(result["description"].endswith("…"))

    def test_starting_price_is_none_when_no_plans(self):
        offering = mp_factories.OfferingFactory()
        self.assertIsNone(serialize_offering_minimal(offering)["starting_price"])

    def test_country_prefers_offering_over_provider(self):
        offering = mp_factories.OfferingFactory(country="DE")
        offering.customer.country = "EE"
        offering.customer.save()
        self.assertEqual(serialize_offering_minimal(offering)["country"], "DE")

    def test_country_falls_back_to_provider_country(self):
        offering = mp_factories.OfferingFactory(country="")
        offering.customer.country = "EE"
        offering.customer.save()
        self.assertEqual(serialize_offering_minimal(offering)["country"], "EE")

    def test_country_is_none_when_unset_everywhere(self):
        offering = mp_factories.OfferingFactory(country="")
        offering.customer.country = ""
        offering.customer.save()
        self.assertIsNone(serialize_offering_minimal(offering)["country"])

    def test_description_html_stripped(self):
        offering = mp_factories.OfferingFactory(
            description="<p>GPU <strong>power</strong> for AI.</p>"
        )
        result = serialize_offering_minimal(offering)
        self.assertEqual(result["description"], "GPU power for AI.")

    def test_has_access_url_flag(self):
        with_url = mp_factories.OfferingFactory(access_url="https://x.example.org/")
        without_url = mp_factories.OfferingFactory(access_url="")
        self.assertTrue(serialize_offering_minimal(with_url)["has_access_url"])
        self.assertFalse(serialize_offering_minimal(without_url)["has_access_url"])


class SearchOfferingsRegistrationTest(TestCase):
    def test_registered(self):
        self.assertIn(ToolName.SEARCH_OFFERINGS, tool_registry)

    def test_definition_shape(self):
        tool = tool_registry.get(ToolName.SEARCH_OFFERINGS)
        self.assertEqual(tool.definition.name, ToolName.SEARCH_OFFERINGS)
        self.assertIn("keyword", tool.definition.inputSchema["properties"])
        self.assertEqual(tool.definition.inputSchema["required"], ["keyword"])


class SearchOfferingsExecuteTest(TestCase):
    def setUp(self):
        self.tool = SearchOfferingsTool()
        self.user = AnonymousUser()

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_keyword_match_returns_offering(self):
        mp_factories.OfferingFactory(
            name="GPU Climate Cluster", shared=True, state=OfferingStates.ACTIVE
        )
        mp_factories.OfferingFactory(
            name="Storage Only", shared=True, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(self.user, {"keyword": "climate"})
        self.assertEqual(result["type"], "success")
        names = [o["name"] for o in result["data"]["offerings"]]
        self.assertEqual(names, ["GPU Climate Cluster"])

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_matches_component_when_description_empty(self):
        """Key invariant: component signal rescues offerings with no description."""
        offering = mp_factories.OfferingFactory(
            name="HPC-Cluster-01",
            description="",
            shared=True,
            state=OfferingStates.ACTIVE,
        )
        mp_factories.OfferingComponentFactory(offering=offering, type="gpu", name="GPU")
        result = self.tool.execute(self.user, {"keyword": "GPU"})
        names = [o["name"] for o in result["data"]["offerings"]]
        self.assertIn("HPC-Cluster-01", names)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_matches_category_title(self):
        cat = mp_factories.CategoryFactory(title="Compute")
        mp_factories.OfferingFactory(
            name="HPC-01", category=cat, shared=True, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(self.user, {"keyword": "compute"})
        names = [o["name"] for o in result["data"]["offerings"]]
        self.assertIn("HPC-01", names)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_no_duplicate_rows_across_joins(self):
        """Broader search uses m2m joins — .distinct() must suppress duplicates."""
        offering = mp_factories.OfferingFactory(
            name="GPU Box", shared=True, state=OfferingStates.ACTIVE
        )
        mp_factories.OfferingComponentFactory(offering=offering, type="gpu", name="GPU")
        mp_factories.OfferingComponentFactory(
            offering=offering, type="vgpu", name="GPU virtual"
        )
        result = self.tool.execute(self.user, {"keyword": "GPU"})
        names = [o["name"] for o in result["data"]["offerings"]]
        self.assertEqual(names.count("GPU Box"), 1)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_excludes_private_offerings(self):
        mp_factories.OfferingFactory(
            name="Private GPU", shared=False, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(self.user, {"keyword": "GPU"})
        self.assertEqual(result["data"]["offerings"], [])

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_excludes_draft_offerings(self):
        mp_factories.OfferingFactory(
            name="Draft GPU", shared=True, state=OfferingStates.DRAFT
        )
        result = self.tool.execute(self.user, {"keyword": "GPU"})
        self.assertEqual(result["data"]["offerings"], [])

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_limit_enforced(self):
        for i in range(5):
            mp_factories.OfferingFactory(
                name=f"GPU {i}", shared=True, state=OfferingStates.ACTIVE
            )
        result = self.tool.execute(self.user, {"keyword": "GPU", "limit": 2})
        self.assertEqual(len(result["data"]["offerings"]), 2)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_category_uuid_filter(self):
        cat_a = mp_factories.CategoryFactory(title="Compute")
        cat_b = mp_factories.CategoryFactory(title="Storage")
        mp_factories.OfferingFactory(
            name="GPU A", category=cat_a, shared=True, state=OfferingStates.ACTIVE
        )
        mp_factories.OfferingFactory(
            name="GPU B", category=cat_b, shared=True, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(
            self.user, {"keyword": "GPU", "category_uuid": str(cat_a.uuid)}
        )
        names = [o["name"] for o in result["data"]["offerings"]]
        self.assertEqual(names, ["GPU A"])

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_category_name_filter(self):
        """Cross-turn recall: LLM passes category title when UUID isn't in context."""
        cat_a = mp_factories.CategoryFactory(title="Compute")
        cat_b = mp_factories.CategoryFactory(title="Storage")
        mp_factories.OfferingFactory(
            name="GPU A", category=cat_a, shared=True, state=OfferingStates.ACTIVE
        )
        mp_factories.OfferingFactory(
            name="GPU B", category=cat_b, shared=True, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(
            self.user, {"keyword": "GPU", "category_name": "Compute"}
        )
        names = [o["name"] for o in result["data"]["offerings"]]
        self.assertEqual(names, ["GPU A"])

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_type_filter(self):
        mp_factories.OfferingFactory(
            name="Slurm A",
            type="Marketplace.Slurm",
            shared=True,
            state=OfferingStates.ACTIVE,
        )
        mp_factories.OfferingFactory(
            name="Basic A",
            type="Marketplace.Basic",
            shared=True,
            state=OfferingStates.ACTIVE,
        )
        result = self.tool.execute(
            self.user, {"keyword": "A", "type": "Marketplace.Slurm"}
        )
        names = [o["name"] for o in result["data"]["offerings"]]
        self.assertEqual(names, ["Slurm A"])

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_empty_result_returns_success_with_empty_list(self):
        result = self.tool.execute(self.user, {"keyword": "nonexistent"})
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["offerings"], [])
        self.assertEqual(result["data"]["total"], 0)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_returns_error_when_flag_disabled(self):
        result = self.tool.execute(self.user, {"keyword": "GPU"})
        self.assertEqual(result["type"], "error")
        self.assertIn("disabled", result["summary"].lower())

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_authenticated_user_still_scope_capped(self):
        mp_factories.OfferingFactory(
            name="Private GPU", shared=False, state=OfferingStates.ACTIVE
        )
        staff = structure_factories.UserFactory(is_staff=True)
        result = self.tool.execute(staff, {"keyword": "GPU"})
        names = [o["name"] for o in result["data"]["offerings"]]
        self.assertNotIn("Private GPU", names)


class SerializeOfferingDetailedTest(TestCase):
    def test_includes_plans_and_components(self):
        offering = mp_factories.OfferingFactory()
        mp_factories.PlanFactory(offering=offering, name="Basic", unit_price=10)
        mp_factories.PlanFactory(
            offering=offering, name="Archived", unit_price=5, archived=True
        )
        mp_factories.OfferingComponentFactory(
            offering=offering,
            type="cpu",
            name="CPU",
        )
        result = serialize_offering_detailed(offering)
        plan_names = [p["name"] for p in result["plans"]]
        self.assertIn("Basic", plan_names)
        self.assertNotIn("Archived", plan_names)
        component_types = [c["type"] for c in result["components"]]
        self.assertIn("cpu", component_types)

    def test_includes_attributes_and_tags(self):
        offering = mp_factories.OfferingFactory(attributes={"gpu_type": "A100"})
        result = serialize_offering_detailed(offering)
        self.assertEqual(result["attributes"], {"gpu_type": "A100"})
        self.assertIsInstance(result["tags"], list)

    def test_includes_access_route_fields(self):
        offering = mp_factories.OfferingFactory(
            access_url="https://provider.example.org/portal/",
            getting_started="Request an account via the provider portal first.",
        )
        result = serialize_offering_detailed(offering)
        self.assertEqual(result["access_url"], "https://provider.example.org/portal/")
        self.assertEqual(
            result["getting_started"],
            "Request an account via the provider portal first.",
        )

    def test_access_route_fields_none_when_blank(self):
        offering = mp_factories.OfferingFactory(access_url="", getting_started="")
        result = serialize_offering_detailed(offering)
        self.assertIsNone(result["access_url"])
        self.assertIsNone(result["getting_started"])

    def test_getting_started_html_stripped(self):
        offering = mp_factories.OfferingFactory(
            getting_started="<p>Step <strong>one</strong>:  register.</p>"
        )
        result = serialize_offering_detailed(offering)
        self.assertEqual(result["getting_started"], "Step one: register.")

    def test_getting_started_capped_with_truncation_marker(self):
        offering = mp_factories.OfferingFactory(getting_started="x" * 2000)
        result = serialize_offering_detailed(offering)
        self.assertLessEqual(len(result["getting_started"]), 1000)
        self.assertTrue(result["getting_started"].endswith("…"))

    def test_getting_started_cap_cuts_on_word_boundary(self):
        offering = mp_factories.OfferingFactory(getting_started="word " * 400)
        result = serialize_offering_detailed(offering)
        self.assertLessEqual(len(result["getting_started"]), 1000)
        self.assertTrue(result["getting_started"].endswith("word…"))

    def test_full_description_html_stripped_and_capped(self):
        offering = mp_factories.OfferingFactory(
            full_description="<p>" + "y" * 3000 + "</p>"
        )
        result = serialize_offering_detailed(offering)
        self.assertNotIn("<p>", result["full_description"])
        self.assertLessEqual(len(result["full_description"]), 2000)
        self.assertTrue(result["full_description"].endswith("…"))


class StripHtmlToTextTest(TestCase):
    def test_block_elements_get_separators(self):
        self.assertEqual(
            strip_html_to_text(
                "<ul><li>Register an account</li><li>Upload your SSH key</li></ul>"
            ),
            "Register an account Upload your SSH key",
        )

    def test_adjacent_paragraphs_get_separators(self):
        self.assertEqual(strip_html_to_text("<p>A</p><p>B</p>"), "A B")

    def test_html_entities_decoded(self):
        self.assertEqual(
            strip_html_to_text("<p>R&amp;D, 5 &lt; 10 nodes</p>"),
            "R&D, 5 < 10 nodes",
        )

    def test_raw_ampersand_survives_round_trip(self):
        self.assertEqual(strip_html_to_text("plain & text"), "plain & text")

    def test_anchor_href_preserved_as_plain_text(self):
        self.assertEqual(
            strip_html_to_text(
                '<p>Register at <a href="https://prov.example/signup">the portal</a>.</p>'
            ),
            "Register at the portal (https://prov.example/signup).",
        )

    def test_mailto_href_preserved(self):
        self.assertEqual(
            strip_html_to_text('<a href="mailto:hpc@example.org">contact us</a>'),
            "contact us (mailto:hpc@example.org)",
        )

    def test_unsafe_scheme_href_dropped(self):
        # getting_started is not sanitised on write — a javascript: href must
        # not be lifted into the plain text.
        self.assertEqual(
            strip_html_to_text('<a href="javascript:alert(1)">click</a>'),
            "click",
        )

    def test_pathological_anchor_input_completes_quickly(self):
        # "<a " runs with no closing ">" make the anchor pre-pass backtrack
        # quadratically, and description/full_description are unbounded
        # provider-editable fields — the stripper must bound the input it
        # scans. Unbounded, this shape takes ~11s at 150 KB.
        start = time.monotonic()
        strip_html_to_text("<a " * 50_000)
        self.assertLess(time.monotonic() - start, 2.0)


class CapTextTest(TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(cap_text("short", 10), "short")

    def test_word_boundary_cut_with_marker(self):
        result = cap_text("word " * 400, 1000)
        self.assertLessEqual(len(result), 1000)
        self.assertTrue(result.endswith("word…"))

    def test_floor_prevents_eating_the_budget(self):
        # A long unbroken token after a leading word must not collapse the
        # output to that word — fall back to a hard slice instead.
        result = cap_text("a " + "b" * 50, 10)
        self.assertGreater(len(result), 5)
        self.assertLessEqual(len(result), 10)
        self.assertTrue(result.endswith("…"))


class GetOfferingRegistrationTest(TestCase):
    def test_registered(self):
        self.assertIn(ToolName.GET_OFFERING, tool_registry)

    def test_definition_shape(self):
        tool = tool_registry.get(ToolName.GET_OFFERING)
        self.assertEqual(tool.definition.name, ToolName.GET_OFFERING)
        # No `required` field — at-least-one of uuid/name is enforced in execute().
        self.assertNotIn("required", tool.definition.inputSchema)
        self.assertIn("uuid", tool.definition.inputSchema["properties"])
        self.assertIn("name", tool.definition.inputSchema["properties"])


class GetOfferingExecuteTest(TestCase):
    def setUp(self):
        self.tool = GetOfferingTool()
        self.user = AnonymousUser()

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_returns_details_with_inline_homeport_url(self):
        offering = mp_factories.OfferingFactory(
            name="GPU A", shared=True, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(self.user, {"uuid": str(offering.uuid)})
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["offering"]["name"], "GPU A")
        self.assertIn("plans", result["data"]["offering"])
        self.assertIn("components", result["data"]["offering"])
        # No tool-side UI block: the LLM emits a closing inline markdown
        # link `[View offering](homeport_url)` from the offering payload.
        self.assertNotIn("ui_component", result)
        self.assertNotIn("ui_data", result)
        self.assertIn("homeport_url", result["data"]["offering"])
        self.assertIn(str(offering.uuid), result["data"]["offering"]["homeport_url"])

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_resolves_offering_by_name(self):
        """Cross-turn recall: LLM passes the name when UUID isn't in context."""
        offering = mp_factories.OfferingFactory(
            name="GPU Training Cluster", shared=True, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(self.user, {"name": "GPU Training Cluster"})
        self.assertEqual(result["type"], "success")
        self.assertEqual(result["data"]["offering"]["name"], "GPU Training Cluster")
        self.assertEqual(result["data"]["offering"]["uuid"], str(offering.uuid))

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_returns_error_when_neither_uuid_nor_name_given(self):
        result = self.tool.execute(self.user, {})
        self.assertEqual(result["type"], "error")

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_returns_error_for_unknown_uuid(self):
        result = self.tool.execute(
            self.user, {"uuid": "00000000-0000-0000-0000-000000000000"}
        )
        self.assertEqual(result["type"], "error")
        self.assertIn("not found by uuid", result["summary"].lower())

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_returns_error_for_private_offering(self):
        offering = mp_factories.OfferingFactory(
            shared=False, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(self.user, {"uuid": str(offering.uuid)})
        self.assertEqual(result["type"], "error")

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_returns_error_when_flag_disabled(self):
        offering = mp_factories.OfferingFactory(
            shared=True, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(self.user, {"uuid": str(offering.uuid)})
        self.assertEqual(result["type"], "error")
        self.assertIn("disabled", result["summary"].lower())

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_summary_directs_to_provider_platform_when_access_url_set(self):
        offering = mp_factories.OfferingFactory(
            name="External HPC",
            shared=True,
            state=OfferingStates.ACTIVE,
            access_url="https://provider.example.org/portal/",
        )
        result = self.tool.execute(self.user, {"uuid": str(offering.uuid)})
        self.assertEqual(result["type"], "success")
        self.assertIn("access_url", result["summary"])
        # The Hub offering-page link must survive alongside the access
        # link — click attribution harvests offering UUIDs from it.
        self.assertIn("homeport_url", result["summary"])
        # access_url is just a link ("Access" in Homeport); it does not mean
        # the Hub order flow is unavailable, and it must not reuse the Hub
        # order button's "Request access" label.
        self.assertIn("[Access](access_url)", result["summary"])
        self.assertNotIn("Request access", result["summary"])
        self.assertNotIn("grants access", result["summary"])

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_summary_keeps_homeport_directive_without_access_url(self):
        offering = mp_factories.OfferingFactory(
            name="Hub HPC", shared=True, state=OfferingStates.ACTIVE, access_url=""
        )
        result = self.tool.execute(self.user, {"uuid": str(offering.uuid)})
        self.assertEqual(result["type"], "success")
        self.assertIn("homeport_url", result["summary"])
        self.assertNotIn("access_url", result["summary"])


class ListCategoriesRegistrationTest(TestCase):
    def test_registered(self):
        self.assertIn(ToolName.LIST_CATEGORIES, tool_registry)

    def test_definition_shape(self):
        tool = tool_registry.get(ToolName.LIST_CATEGORIES)
        self.assertEqual(tool.definition.name, ToolName.LIST_CATEGORIES)
        self.assertEqual(tool.definition.inputSchema["required"], [])


class ListCategoriesExecuteTest(TestCase):
    def setUp(self):
        self.tool = ListCategoriesTool()
        self.user = AnonymousUser()

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_returns_only_categories_with_public_offerings(self):
        cat_a = mp_factories.CategoryFactory(title="Compute")
        cat_b = mp_factories.CategoryFactory(title="Storage")
        cat_c = mp_factories.CategoryFactory(title="Empty")
        mp_factories.OfferingFactory(
            category=cat_a, shared=True, state=OfferingStates.ACTIVE
        )
        mp_factories.OfferingFactory(
            category=cat_b, shared=True, state=OfferingStates.PAUSED
        )
        mp_factories.OfferingFactory(
            category=cat_c, shared=False, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(self.user, {})
        titles = [c["title"] for c in result["data"]["categories"]]
        self.assertCountEqual(titles, ["Compute", "Storage"])
        self.assertNotIn("Empty", titles)
        # Fix 1: no tool-side markdown block — LLM narrates from data.
        self.assertNotIn("ui_component", result)
        self.assertNotIn("ui_data", result)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_includes_offering_counts(self):
        cat = mp_factories.CategoryFactory(title="Compute")
        mp_factories.OfferingFactory(
            category=cat, shared=True, state=OfferingStates.ACTIVE
        )
        mp_factories.OfferingFactory(
            category=cat, shared=True, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(self.user, {})
        compute = next(
            c for c in result["data"]["categories"] if c["title"] == "Compute"
        )
        self.assertEqual(compute["offering_count"], 2)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_returns_error_when_flag_disabled(self):
        result = self.tool.execute(self.user, {})
        self.assertEqual(result["type"], "error")


class CompareOfferingsRegistrationTest(TestCase):
    def test_registered(self):
        self.assertIn(ToolName.COMPARE_OFFERINGS, tool_registry)

    def test_definition_shape(self):
        tool = tool_registry.get(ToolName.COMPARE_OFFERINGS)
        self.assertEqual(tool.definition.name, ToolName.COMPARE_OFFERINGS)
        schema = tool.definition.inputSchema
        # Schema exposes both arguments; ≥2 total is enforced in execute().
        self.assertNotIn("required", schema)
        self.assertEqual(schema["properties"]["uuids"]["type"], "array")
        self.assertEqual(schema["properties"]["names"]["type"], "array")
        self.assertNotIn("maxItems", schema["properties"]["uuids"])

    def test_names_only_call_is_accepted(self):
        """LLM can pass just names when UUIDs aren't in its context."""
        schema = tool_registry.get(ToolName.COMPARE_OFFERINGS).definition.inputSchema
        self.assertIn("names", schema["properties"])


class CompareOfferingsExecuteTest(TestCase):
    def setUp(self):
        self.tool = CompareOfferingsTool()
        self.user = AnonymousUser()

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_two_public_offerings_expose_homeport_url_per_row(self):
        a = mp_factories.OfferingFactory(
            name="GPU A", shared=True, state=OfferingStates.ACTIVE
        )
        b = mp_factories.OfferingFactory(
            name="GPU B", shared=True, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(self.user, {"uuids": [str(a.uuid), str(b.uuid)]})
        self.assertEqual(result["type"], "success")
        self.assertEqual(len(result["data"]["offerings"]), 2)
        # No tool-side UI block: the LLM renders the comparison as a
        # markdown table and follows it with `View [A](url) · [B](url)`
        # using each offering's `homeport_url` field.
        self.assertNotIn("ui_component", result)
        self.assertNotIn("ui_data", result)
        urls = {o["homeport_url"] for o in result["data"]["offerings"]}
        self.assertEqual(len(urls), 2)
        self.assertTrue(all(str(a.uuid) in u or str(b.uuid) in u for u in urls))

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_unknown_uuid_dropped_with_note(self):
        a = mp_factories.OfferingFactory(
            name="GPU A", shared=True, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(
            self.user,
            {"uuids": [str(a.uuid), "00000000-0000-0000-0000-000000000000"]},
        )
        self.assertEqual(result["type"], "success")
        self.assertEqual(len(result["data"]["offerings"]), 1)
        self.assertIn("1 unavailable", result["summary"])

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_all_unknown_returns_error(self):
        result = self.tool.execute(
            self.user,
            {
                "uuids": [
                    "00000000-0000-0000-0000-000000000000",
                    "00000000-0000-0000-0000-000000000001",
                ]
            },
        )
        self.assertEqual(result["type"], "error")

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_no_cap_on_offering_count(self):
        """Tool accepts any number ≥ 2 — readability is the LLM's concern."""
        offerings = [
            mp_factories.OfferingFactory(
                name=f"GPU {i}", shared=True, state=OfferingStates.ACTIVE
            )
            for i in range(6)
        ]
        result = self.tool.execute(
            self.user, {"uuids": [str(o.uuid) for o in offerings]}
        )
        self.assertEqual(result["type"], "success")
        self.assertEqual(len(result["data"]["offerings"]), 6)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_render_as_table_flag_for_small_sets(self):
        a = mp_factories.OfferingFactory(
            name="GPU A", shared=True, state=OfferingStates.ACTIVE
        )
        b = mp_factories.OfferingFactory(
            name="GPU B", shared=True, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(self.user, {"uuids": [str(a.uuid), str(b.uuid)]})
        self.assertTrue(result["data"]["render_as_table"])

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_render_as_table_flag_false_at_four_or_more(self):
        offerings = [
            mp_factories.OfferingFactory(
                name=f"GPU {i}", shared=True, state=OfferingStates.ACTIVE
            )
            for i in range(4)
        ]
        result = self.tool.execute(
            self.user, {"uuids": [str(o.uuid) for o in offerings]}
        )
        self.assertFalse(result["data"]["render_as_table"])

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_resolves_offerings_by_name(self):
        """Cross-turn recall: LLM passes names when UUIDs aren't in its context."""
        mp_factories.OfferingFactory(
            name="GPU Training Cluster", shared=True, state=OfferingStates.ACTIVE
        )
        mp_factories.OfferingFactory(
            name="SLURM Batch Computing", shared=True, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(
            self.user,
            {"names": ["GPU Training Cluster", "SLURM Batch Computing"]},
        )
        self.assertEqual(result["type"], "success")
        self.assertEqual(len(result["data"]["offerings"]), 2)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_resolves_mix_of_uuids_and_names(self):
        a = mp_factories.OfferingFactory(
            name="GPU A", shared=True, state=OfferingStates.ACTIVE
        )
        mp_factories.OfferingFactory(
            name="GPU B", shared=True, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(
            self.user,
            {"uuids": [str(a.uuid)], "names": ["GPU B"]},
        )
        self.assertEqual(result["type"], "success")
        names = {o["name"] for o in result["data"]["offerings"]}
        self.assertEqual(names, {"GPU A", "GPU B"})

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_returns_error_when_total_below_two(self):
        offering = mp_factories.OfferingFactory(
            name="Solo", shared=True, state=OfferingStates.ACTIVE
        )
        result = self.tool.execute(self.user, {"uuids": [str(offering.uuid)]})
        self.assertEqual(result["type"], "error")

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_returns_error_when_flag_disabled(self):
        result = self.tool.execute(self.user, {"uuids": ["x", "y"]})
        self.assertEqual(result["type"], "error")


class MarketplaceToolsOpenAIFormatTest(TestCase):
    def test_all_marketplace_tools_in_openai_format(self):
        names = [
            ToolName.SEARCH_OFFERINGS,
            ToolName.GET_OFFERING,
            ToolName.LIST_CATEGORIES,
            ToolName.COMPARE_OFFERINGS,
        ]
        openai_tools = tool_registry.get_openai_tools(names)
        self.assertEqual(len(openai_tools), 4)
        for spec in openai_tools:
            self.assertEqual(spec["type"], "function")
            self.assertIn("name", spec["function"])
            self.assertIn("description", spec["function"])
            self.assertIn("parameters", spec["function"])
            self.assertEqual(spec["function"]["parameters"]["type"], "object")
