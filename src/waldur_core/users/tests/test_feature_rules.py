from unittest import mock

import requests
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core import features
from waldur_core.core.models import Feature
from waldur_core.logging.models import Event
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.users import feature_rules, micro_apps_registry, models

# A user-scoped flag nobody turns on in these tests except through rules.
TARGETED = "marketplace.realtime_updates"
# A flag that stays one value for the whole portal.
DEPLOYMENT = "marketplace.call_only"

RULES_URL = "http://testserver" + reverse("feature-rule-list")
APPS_URL = "http://testserver" + reverse("micro-app-setting-list")
ME_URL = "http://testserver" + reverse("user-me")


def rule_url(rule, action=None):
    name = f"feature-rule-{action}" if action else "feature-rule-detail"
    return "http://testserver" + reverse(name, kwargs={"uuid": rule.uuid.hex})


def create_rule(**kwargs):
    m2m = {
        name: kwargs.pop(name)
        for name in ("users", "customers", "organization_groups")
        if name in kwargs
    }
    kwargs.setdefault("feature_key", TARGETED)
    rule = models.FeatureRule.objects.create(**kwargs)
    for name, values in m2m.items():
        getattr(rule, name).set(values)
    return rule


class BaseFeatureRuleTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.staff = self.fixture.staff
        self.support = self.fixture.global_support
        self.user = structure_factories.UserFactory()
        self.other = structure_factories.UserFactory()

    def me_features(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(ME_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data


class FeatureScopeTest(BaseFeatureRuleTest):
    def test_metadata_names_each_flag_scope(self):
        response = self.client.get("/api/metadata/features/")
        items = {
            f"{section['key']}.{item['key']}": item["scope"]
            for section in response.data["features"]
            for item in section["items"]
        }
        self.assertEqual(items[TARGETED], features.SCOPE_USER)
        self.assertEqual(items[DEPLOYMENT], features.SCOPE_DEPLOYMENT)
        self.assertEqual(items["deployment.enable_cookie_notice"], "deployment")

    def test_rule_for_deployment_scoped_flag_is_refused(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            RULES_URL,
            {"feature_key": DEPLOYMENT, "users": [self.user.uuid.hex]},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("feature_key", response.data)

    def test_rule_for_unknown_flag_is_refused(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            RULES_URL, {"feature_key": "user.nope", "users": [self.user.uuid.hex]}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_deployment_scoped_flag_ignores_rules_made_behind_the_api(self):
        create_rule(feature_key=DEPLOYMENT, users=[self.user])
        self.assertFalse(features.is_enabled(DEPLOYMENT, self.user))


class ConfigurationStaysGlobalTest(BaseFeatureRuleTest):
    def test_configuration_is_the_same_for_a_matched_user_and_anonymous(self):
        create_rule(users=[self.user])
        cache.delete("API_CONFIGURATION")
        anonymous = self.client.get("/api/configuration/").data["FEATURES"]
        cache.delete("API_CONFIGURATION")
        self.client.force_authenticate(self.user)
        signed_in = self.client.get("/api/configuration/").data["FEATURES"]
        self.assertEqual(anonymous, signed_in)
        self.assertFalse(signed_in["marketplace"]["realtime_updates"])


class MeFeaturesTest(BaseFeatureRuleTest):
    def test_rule_turns_flag_on_for_the_matched_user_only(self):
        rule = create_rule(users=[self.user])

        self.assertTrue(
            self.me_features(self.user)["features"]["marketplace"]["realtime_updates"]
        )
        self.assertFalse(
            self.me_features(self.other)["features"]["marketplace"]["realtime_updates"]
        )

        rule.is_active = False
        rule.save()
        self.assertFalse(
            self.me_features(self.user)["features"]["marketplace"]["realtime_updates"]
        )

    def test_global_value_applies_to_everyone(self):
        Feature.objects.create(key=TARGETED, value=True)
        self.assertTrue(
            self.me_features(self.other)["features"]["marketplace"]["realtime_updates"]
        )

    def test_only_user_scoped_flags_are_listed(self):
        data = self.me_features(self.user)["features"]
        self.assertIn("realtime_updates", data["marketplace"])
        self.assertNotIn("call_only", data["marketplace"])
        self.assertNotIn("deployment", data)

    def test_me_never_shows_rules(self):
        create_rule(users=[self.user], description="pilot group")
        self.assertNotIn("pilot group", str(self.me_features(self.user)))

    def test_is_enabled_takes_the_user(self):
        create_rule(users=[self.user])
        self.assertTrue(features.is_enabled(TARGETED, self.user))
        self.assertFalse(features.is_enabled(TARGETED, self.other))
        self.assertFalse(features.is_enabled(TARGETED))


class CriteriaTest(BaseFeatureRuleTest):
    def assertTargets(self, rule, matched, unmatched):
        self.assertIn(TARGETED, feature_rules.targeted_keys(matched))
        self.assertNotIn(TARGETED, feature_rules.targeted_keys(unmatched))

    def test_explicit_user(self):
        self.assertTargets(create_rule(users=[self.user]), self.user, self.other)

    def test_customer_through_a_project_role(self):
        rule = create_rule(customers=[self.fixture.customer])
        self.assertTargets(rule, self.fixture.admin, self.other)

    def test_customer_through_a_customer_role(self):
        rule = create_rule(customers=[self.fixture.customer])
        self.assertTargets(rule, self.fixture.owner, self.other)

    def test_organization_group(self):
        group = structure_factories.OrganizationGroupFactory()
        self.fixture.customer.organization_groups.add(group)
        rule = create_rule(organization_groups=[group])
        self.assertTargets(rule, self.fixture.owner, self.other)

    def test_role(self):
        rule = create_rule(roles=[CustomerRole.OWNER.name])
        self.assertTargets(rule, self.fixture.owner, self.fixture.admin)

    def test_email_pattern(self):
        self.user.email = "alice@pilot.example.com"
        self.user.save()
        rule = create_rule(user_email_patterns=[".*@pilot.example.com"])
        self.assertTargets(rule, self.user, self.other)

    def test_identity_source(self):
        self.user.identity_source = "eduteams"
        self.user.save()
        rule = create_rule(user_identity_sources=["eduteams"])
        self.assertTargets(rule, self.user, self.other)

    def test_claim(self):
        self.user.details = {"entitlement": ["urn:example:pilot#authority"]}
        self.user.save()
        rule = create_rule(user_claims={"entitlement": ["urn:example:pilot*"]})
        self.assertTargets(rule, self.user, self.other)

    def test_every_configured_group_must_match(self):
        # Listed, but not holding the role the rule also asks for.
        rule = create_rule(users=[self.user], roles=[ProjectRole.ADMIN.name])
        self.assertNotIn(TARGETED, feature_rules.targeted_keys(self.user))
        # Holding the role, but not listed.
        self.assertNotIn(TARGETED, feature_rules.targeted_keys(self.fixture.admin))
        rule.users.add(self.fixture.admin)
        self.assertIn(TARGETED, feature_rules.targeted_keys(self.fixture.admin))


class RuleValidationTest(BaseFeatureRuleTest):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.staff)

    def test_rule_without_criteria_matches_no_one(self):
        response = self.client.post(RULES_URL, {"feature_key": TARGETED}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn(TARGETED, feature_rules.targeted_keys(self.user))
        self.assertNotIn(TARGETED, feature_rules.targeted_keys(self.other))

    def test_clearing_the_last_criterion_turns_the_rule_off(self):
        rule = create_rule(users=[self.user])
        self.assertIn(TARGETED, feature_rules.targeted_keys(self.user))
        response = self.client.patch(rule_url(rule), {"users": []}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(TARGETED, feature_rules.targeted_keys(self.user))
        self.assertNotIn(TARGETED, feature_rules.targeted_keys(self.other))

    def test_deleting_the_only_listed_group_does_not_widen_the_rule(self):
        group = structure_factories.OrganizationGroupFactory()
        self.fixture.customer.organization_groups.add(group)
        rule = create_rule(organization_groups=[group])
        self.assertIn(TARGETED, feature_rules.targeted_keys(self.fixture.owner))
        group.delete()
        self.assertNotIn(TARGETED, feature_rules.targeted_keys(self.fixture.owner))
        self.assertNotIn(TARGETED, feature_rules.targeted_keys(self.other))
        response = self.client.patch(
            rule_url(rule), {"is_active": False}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_bare_wildcard_claim_is_refused(self):
        response = self.client.post(
            RULES_URL,
            {"feature_key": TARGETED, "user_claims": {"entitlement": ["*"]}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_role_is_refused(self):
        response = self.client.post(
            RULES_URL,
            {"feature_key": TARGETED, "roles": ["NO.SUCH_ROLE"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_creates_a_rule_and_an_event_is_logged(self):
        response = self.client.post(
            RULES_URL,
            {
                "feature_key": TARGETED,
                "users": [self.user.uuid.hex],
                "customers": [self.fixture.customer.uuid.hex],
                "description": "pilot",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        rule = models.FeatureRule.objects.get(uuid=response.data["uuid"])
        self.assertEqual(rule.created_by, self.staff)
        self.assertEqual(
            response.data["users_summary"][0]["username"], self.user.username
        )
        self.assertTrue(
            Event.objects.filter(event_type="feature_rule_created").exists()
        )


class RulePermissionsTest(BaseFeatureRuleTest):
    def test_support_reads_but_cannot_write(self):
        create_rule(users=[self.user])
        self.client.force_authenticate(self.support)
        self.assertEqual(self.client.get(RULES_URL).status_code, status.HTTP_200_OK)
        response = self.client.post(
            RULES_URL,
            {"feature_key": TARGETED, "users": [self.user.uuid.hex]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_other_users_cannot_read(self):
        rule = create_rule(users=[self.user])
        self.client.force_authenticate(self.user)
        self.assertEqual(
            self.client.get(RULES_URL).status_code, status.HTTP_403_FORBIDDEN
        )
        self.assertEqual(
            self.client.get(rule_url(rule)).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_filter_by_feature_key(self):
        create_rule(users=[self.user])
        create_rule(feature_key="micro_apps.provider-calls", users=[self.user])
        self.client.force_authenticate(self.staff)
        response = self.client.get(RULES_URL, {"feature_key": TARGETED})
        self.assertEqual(len(response.data), 1)


class TestMatchTest(BaseFeatureRuleTest):
    def test_breakdown_names_each_criterion(self):
        rule = create_rule(users=[self.user], roles=[CustomerRole.OWNER.name])
        self.client.force_authenticate(self.support)
        response = self.client.post(
            rule_url(rule, "test-match"), {"user_uuid": self.user.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["matched"])
        checks = {check["name"]: check for check in response.data["filters"]}
        self.assertTrue(checks["users"]["matched"])
        self.assertEqual(checks["users"]["rule_value"], [self.user.username])
        self.assertFalse(checks["roles"]["matched"])
        self.assertFalse(checks["organizations"]["configured"])
        self.assertIsNone(checks["organizations"]["rule_value"])

    def test_breakdown_names_organizations_on_both_sides(self):
        group = structure_factories.OrganizationGroupFactory(name="Pilot sites")
        rule = create_rule(
            customers=[self.fixture.customer], organization_groups=[group]
        )
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            rule_url(rule, "test-match"), {"user_uuid": self.fixture.owner.uuid.hex}
        )
        check = {c["name"]: c for c in response.data["filters"]}["organizations"]
        self.assertTrue(check["matched"])
        self.assertIn(self.fixture.customer.name, check["rule_value"])
        self.assertIn("Pilot sites (organization group)", check["rule_value"])
        self.assertEqual(check["user_value"], [self.fixture.customer.name])
        # Listed users are named, never counted, and not configured stays empty.
        self.assertIsNone(
            {c["name"]: c for c in response.data["filters"]}["users"]["rule_value"]
        )

    def test_unknown_user(self):
        rule = create_rule(users=[self.user])
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            rule_url(rule, "test-match"), {"user_uuid": "0" * 32}
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class MatchedUsersTest(BaseFeatureRuleTest):
    def matched(self, rule):
        self.client.force_authenticate(self.staff)
        response = self.client.get(rule_url(rule, "matched-users"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_counts_members_of_a_customer(self):
        rule = create_rule(customers=[self.fixture.customer])
        self.fixture.owner
        self.fixture.admin
        data = self.matched(rule)
        self.assertEqual(data["count"], 2)
        self.assertTrue(data["complete"])

    def test_rule_without_criteria_matches_no_one(self):
        rule = create_rule()
        self.user
        self.other
        data = self.matched(rule)
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["users"], [])
        self.assertTrue(data["complete"])

    def test_identity_attributes_are_checked(self):
        self.user.email = "a@pilot.example.com"
        self.user.save()
        data = self.matched(create_rule(user_email_patterns=[".*@pilot.example.com"]))
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["users"][0]["uuid"], self.user.uuid.hex)


@mock.patch(
    "waldur_core.users.micro_apps_registry.deployed_micro_app_names",
    return_value=None,
)
class MicroAppsTest(BaseFeatureRuleTest):
    def apps(self, user):
        return self.me_features(user)["micro_apps"]

    def test_without_settings_no_app_is_listed(self, _registry):
        self.assertEqual(self.apps(self.user), {})

    def test_modes(self, _registry):
        models.MicroAppSetting.objects.create(name="everyone-app", mode="all")
        models.MicroAppSetting.objects.create(name="off-app", mode="off")
        models.MicroAppSetting.objects.create(name="pilot-app", mode="audience")
        create_rule(feature_key="micro_apps.pilot-app", users=[self.user])

        self.assertEqual(
            self.apps(self.user),
            {"everyone-app": True, "off-app": False, "pilot-app": True},
        )
        self.assertEqual(
            self.apps(self.other),
            {"everyone-app": True, "off-app": False, "pilot-app": False},
        )

    def test_staff_manages_settings_and_removing_one_drops_its_rules(self, _registry):
        self.client.force_authenticate(self.staff)
        response = self.client.post(APPS_URL, {"name": "pilot-app", "mode": "audience"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        create_rule(feature_key="micro_apps.pilot-app", users=[self.user])

        response = self.client.delete(response.data["url"])
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.FeatureRule.objects.filter(
                feature_key="micro_apps.pilot-app"
            ).exists()
        )

    def test_name_cannot_change(self, _registry):
        setting = models.MicroAppSetting.objects.create(name="pilot-app")
        self.client.force_authenticate(self.staff)
        url = "http://testserver" + reverse(
            "micro-app-setting-detail", kwargs={"uuid": setting.uuid.hex}
        )
        response = self.client.patch(url, {"name": "other-app"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_support_cannot_write(self, _registry):
        self.client.force_authenticate(self.support)
        response = self.client.post(APPS_URL, {"name": "pilot-app", "mode": "all"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_invalid_name_is_refused(self, _registry):
        self.client.force_authenticate(self.staff)
        response = self.client.post(APPS_URL, {"name": "Bad Name", "mode": "all"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class UserFeaturesExplainTest(BaseFeatureRuleTest):
    def url(self, user):
        return "http://testserver" + reverse(
            "user-features", kwargs={"uuid": user.uuid.hex}
        )

    def test_reason_for_each_flag(self):
        rule = create_rule(users=[self.user])
        Feature.objects.create(key="dashboard.usage_treemap", value=True)
        self.client.force_authenticate(self.support)
        response = self.client.get(self.url(self.user))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        states = {state["key"]: state for state in response.data["features"]}
        self.assertEqual(states[TARGETED]["source"], "rule")
        self.assertEqual(
            [str(u).replace("-", "") for u in states[TARGETED]["rules"]],
            [rule.uuid.hex],
        )
        self.assertEqual(states["dashboard.usage_treemap"]["source"], "global")
        self.assertEqual(states[DEPLOYMENT]["source"], "off")

    def test_only_staff_and_support(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url(self.user))
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )


class MicroAppRegistryTest(BaseFeatureRuleTest):
    def setUp(self):
        super().setUp()
        cache.delete(micro_apps_registry.CACHE_KEY)
        self.client.force_authenticate(self.staff)

    def tearDown(self):
        cache.delete(micro_apps_registry.CACHE_KEY)

    @mock.patch("waldur_core.users.micro_apps_registry.requests.get")
    def test_refuses_an_app_the_portal_does_not_have(self, get):
        get.return_value.json.return_value = {"apps": [{"name": "ai-inference"}]}
        response = self.client.post(APPS_URL, {"name": "ai-inferense", "mode": "off"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.post(APPS_URL, {"name": "ai-inference", "mode": "off"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # Read once, then from the cache.
        self.assertEqual(get.call_count, 1)

    @mock.patch(
        "waldur_core.users.micro_apps_registry.requests.get",
        side_effect=requests.ConnectionError("down"),
    )
    def test_takes_any_name_while_the_registry_is_unreadable(self, _get):
        response = self.client.post(APPS_URL, {"name": "any-app", "mode": "all"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_config(HOMEPORT_URL="")
    def test_knows_nothing_without_a_portal_url(self):
        self.assertIsNone(micro_apps_registry.deployed_micro_app_names())


class OrphanedRulesTest(BaseFeatureRuleTest):
    def test_rules_without_effect_are_marked_and_filtered(self):
        live = create_rule(users=[self.user])
        moved = create_rule(feature_key=DEPLOYMENT, users=[self.user])
        no_setting = create_rule(feature_key="micro_apps.gone-app", users=[self.user])
        models.MicroAppSetting.objects.create(name="kept-app", mode="audience")
        kept = create_rule(feature_key="micro_apps.kept-app", users=[self.user])

        self.client.force_authenticate(self.staff)
        orphaned = self.client.get(RULES_URL, {"orphaned": "true"}).data
        self.assertEqual(
            {rule["uuid"] for rule in orphaned}, {moved.uuid.hex, no_setting.uuid.hex}
        )
        self.assertTrue(all(rule["is_orphaned"] for rule in orphaned))
        rest = self.client.get(RULES_URL, {"orphaned": "false"}).data
        self.assertEqual(
            {rule["uuid"] for rule in rest}, {live.uuid.hex, kept.uuid.hex}
        )
        self.assertFalse(any(rule["is_orphaned"] for rule in rest))
