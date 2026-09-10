from unittest.mock import patch

from constance.test.unittest import override_config
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from waldur_autoprovisioning import models
from waldur_autoprovisioning.reconciliation import (
    REVOKE_REASON,
    reconcile_autoprovisioned_roles,
)
from waldur_autoprovisioning.tests import factories as autoprovisioning_factories
from waldur_core.core.models import User
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.models import CustomerRoleConcealment, UserRole
from waldur_core.structure.tests import factories as structure_factories


def _user(**kwargs):
    return User.objects.create(
        username=kwargs.pop("username", "u"),
        email=kwargs.pop("email", "u@example.com"),
        **kwargs,
    )


def _active_roles(user):
    return set(
        UserRole.objects.filter(user=user, is_active=True).values_list(
            "role__name", flat=True
        )
    )


@patch("waldur_autoprovisioning.handlers.process_order_on_commit")
class ClaimMatchingTest(TestCase):
    """Rule.evaluate_for_user, claims filter."""

    def _rule(self, **kwargs):
        kwargs.setdefault("plan", None)
        return autoprovisioning_factories.RuleFactory(**kwargs)

    def test_claim_from_details_matches(self, _):
        rule = self._rule(user_claims={"roles": ["acme-owner"]})
        user = _user(details={"roles": ["acme-owner", "unrelated"]})
        self.assertTrue(models.Rule.evaluate_for_user(rule, user).matched)

    def test_claim_absent_does_not_match(self, _):
        rule = self._rule(user_claims={"roles": ["acme-owner"]})
        user = _user(details={"roles": ["someone-else"]})
        self.assertFalse(models.Rule.evaluate_for_user(rule, user).matched)

    def test_scalar_claim_value_is_accepted(self, _):
        rule = self._rule(user_claims={"roles": ["acme-owner"]})
        user = _user(details={"roles": "acme-owner"})
        self.assertTrue(models.Rule.evaluate_for_user(rule, user).matched)

    def test_values_within_one_claim_are_or(self, _):
        rule = self._rule(user_claims={"roles": ["a", "b"]})
        self.assertTrue(
            models.Rule.evaluate_for_user(rule, _user(details={"roles": ["b"]})).matched
        )

    def test_claims_are_and(self, _):
        rule = self._rule(user_claims={"roles": ["a"], "unit": ["hpc"]})
        both = _user(username="both", details={"roles": ["a"], "unit": ["hpc"]})
        one = _user(username="one", details={"roles": ["a"]})
        self.assertTrue(models.Rule.evaluate_for_user(rule, both).matched)
        self.assertFalse(models.Rule.evaluate_for_user(rule, one).matched)

    def test_trailing_star_matches_by_prefix(self, _):
        rule = self._rule(
            user_claims={"entitlements": ["urn:mace:example.org:group:hpc-*"]}
        )
        user = _user(
            details={
                "entitlements": ["urn:mace:example.org:group:hpc-eu#idp.example.org"]
            }
        )
        self.assertTrue(models.Rule.evaluate_for_user(rule, user).matched)

    def test_prefix_does_not_match_a_different_branch(self, _):
        rule = self._rule(
            user_claims={"entitlements": ["urn:mace:example.org:group:hpc-*"]}
        )
        user = _user(details={"entitlements": ["urn:mace:example.org:group:admin"]})
        self.assertFalse(models.Rule.evaluate_for_user(rule, user).matched)

    def test_falls_back_to_mapped_user_column(self, _):
        """A claim mapped through attribute_mapping lands on a User column, not
        in details; the rule must still match it."""
        rule = self._rule(user_claims={"affiliations": ["staff@example.com"]})
        user = _user(affiliations=["staff@example.com"])
        self.assertTrue(models.Rule.evaluate_for_user(rule, user).matched)

    def test_does_not_fall_back_to_arbitrary_user_attributes(self, _):
        """The claim name is administrator-supplied; it must not be able to
        address model internals."""
        rule = self._rule(user_claims={"is_staff": ["True"]})
        user = _user(is_staff=True)
        self.assertFalse(models.Rule.evaluate_for_user(rule, user).matched)

    def test_claims_are_not_satisfied_by_an_unrelated_email_match(self, _):
        """Claims sit in the AND group: a rule that grants a role off a claim
        must not be satisfied because the basic OR group passed."""
        rule = self._rule(
            user_email_patterns=[r".+@example\.com"],
            user_claims={"roles": ["acme-owner"]},
        )
        user = _user(email="someone@example.com", details={"roles": ["nope"]})
        self.assertFalse(models.Rule.evaluate_for_user(rule, user).matched)

    def test_filter_result_is_reported(self, _):
        rule = self._rule(user_claims={"roles": ["acme-owner"]})
        user = _user(details={"roles": ["nope"]})
        result = models.Rule.evaluate_for_user(rule, user)
        claims_fr = next(fr for fr in result.filter_results if fr.name == "claims")
        self.assertTrue(claims_fr.configured)
        self.assertFalse(claims_fr.matched)
        self.assertEqual(claims_fr.user_value, {"roles": ["nope"]})
        self.assertEqual(claims_fr.rule_value, {"roles": ["acme-owner"]})
        self.assertIn("roles", claims_fr.reason)

    def test_rule_without_claims_is_unaffected(self, _):
        rule = self._rule(user_email_patterns=[r".+@example\.com"])
        user = _user(email="hit@example.com")
        result = models.Rule.evaluate_for_user(rule, user)
        self.assertTrue(result.matched)
        claims_fr = next(fr for fr in result.filter_results if fr.name == "claims")
        self.assertFalse(claims_fr.configured)


@patch("waldur_autoprovisioning.handlers.process_order_on_commit")
class ReconciliationTest(TestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()

    def _rule(self, **kwargs):
        kwargs.setdefault("plan", None)
        kwargs.setdefault("customer", self.customer)
        kwargs.setdefault("create_project", False)
        kwargs.setdefault("customer_role", CustomerRole.OWNER)
        return autoprovisioning_factories.RuleFactory(**kwargs)

    def test_matching_user_gains_the_organization_role(self, _):
        rule = self._rule(user_claims={"roles": ["acme-owner"]})
        user = _user(details={"roles": ["acme-owner"]})

        # The grant already happened on user creation (handle_new_user runs the
        # full provisioning pass), so reconciling again is a no-op — which is
        # the property worth pinning down.
        result = reconcile_autoprovisioned_roles(user)

        self.assertEqual(_active_roles(user), {CustomerRole.OWNER.name})
        self.assertEqual(result["granted"], [])
        grant = UserRole.objects.get(user=user, is_active=True)
        self.assertEqual(grant.source, rule.grant_source)

    def test_rule_added_after_the_user_is_applied_on_next_sync(self, _):
        """The case the login hook exists for: the account predates the rule."""
        user = _user(details={"roles": ["acme-owner"]})
        self.assertEqual(_active_roles(user), set())

        self._rule(user_claims={"roles": ["acme-owner"]})
        result = reconcile_autoprovisioned_roles(user)

        self.assertEqual(len(result["granted"]), 1)
        self.assertEqual(_active_roles(user), {CustomerRole.OWNER.name})

    def test_non_matching_user_gains_nothing(self, _):
        self._rule(user_claims={"roles": ["acme-owner"]})
        user = _user(details={"roles": ["other"]})

        reconcile_autoprovisioned_roles(user)

        self.assertEqual(_active_roles(user), set())

    def test_reconciliation_is_idempotent(self, _):
        self._rule(user_claims={"roles": ["acme-owner"]})
        user = _user(details={"roles": ["acme-owner"]})

        reconcile_autoprovisioned_roles(user)
        second = reconcile_autoprovisioned_roles(user)

        self.assertEqual(second["granted"], [])
        self.assertEqual(UserRole.objects.filter(user=user, is_active=True).count(), 1)

    def test_losing_the_claim_revokes_when_opted_in(self, _):
        rule = self._rule(
            name="acme owners",
            user_claims={"roles": ["acme-owner"]},
            revoke_when_unmatched=True,
        )
        user = _user(details={"roles": ["acme-owner"]})
        reconcile_autoprovisioned_roles(user)
        self.assertEqual(_active_roles(user), {CustomerRole.OWNER.name})

        user.details = {"roles": []}
        user.save()
        result = reconcile_autoprovisioned_roles(user)

        self.assertEqual(_active_roles(user), set())
        self.assertEqual(len(result["revoked"]), 1)
        revoked = UserRole.objects.get(user=user)
        self.assertFalse(revoked.is_active)
        # The audit trail names the rule: "a system revoked this" is not an
        # answer an administrator can act on.
        self.assertEqual(revoked.revoke_reason, REVOKE_REASON.format(name=rule.name))

    def test_regaining_the_claim_restores_the_same_grant(self, _):
        """Regaining a claim reactivates the grant instead of duplicating it.

        `(user, scope, role)` is unique while active elsewhere in the codebase —
        `validate_role_grant` refuses a second one — and `User` follows the same
        reactivate-in-place convention when it regains a role.
        """
        self._rule(user_claims={"roles": ["acme-owner"]}, revoke_when_unmatched=True)
        user = _user(details={"roles": ["acme-owner"]})
        first = UserRole.objects.get(user=user, is_active=True)

        user.details = {"roles": []}
        user.save()
        reconcile_autoprovisioned_roles(user)
        self.assertEqual(_active_roles(user), set())

        user.details = {"roles": ["acme-owner"]}
        user.save()
        result = reconcile_autoprovisioned_roles(user)

        self.assertEqual(len(result["granted"]), 1)
        self.assertEqual(_active_roles(user), {CustomerRole.OWNER.name})
        self.assertEqual(UserRole.objects.filter(user=user).count(), 1)
        restored = UserRole.objects.get(user=user)
        self.assertEqual(restored.pk, first.pk)
        # The revocation is cleared, so the row does not claim to be both.
        self.assertEqual(restored.revoke_reason, "")
        self.assertIsNone(restored.revoked_by)

    def test_restoring_respects_concealment_added_meanwhile(self, _):
        """restore() skips the org-scoping policy, so reconciliation checks it:
        a role concealed since the grant was made must not come back."""
        self._rule(user_claims={"roles": ["acme-owner"]}, revoke_when_unmatched=True)
        user = _user(details={"roles": ["acme-owner"]})
        user.details = {"roles": []}
        user.save()
        reconcile_autoprovisioned_roles(user)

        CustomerRoleConcealment.objects.create(
            role=CustomerRole.OWNER,
            content_type=ContentType.objects.get_for_model(type(self.customer)),
            object_id=self.customer.id,
        )
        user.details = {"roles": ["acme-owner"]}
        user.save()
        reconcile_autoprovisioned_roles(user)

        self.assertEqual(_active_roles(user), set())

    def test_a_persons_revoked_grant_is_not_restored(self, _):
        """Reconciliation stays out of rows it did not issue, in both
        directions: it will not revoke a person's grant, and it will not
        resurrect one either — it issues its own instead."""
        self._rule(user_claims={"roles": ["acme-owner"]})
        user = _user(details={"roles": ["other"]})
        self.customer.add_user(user, CustomerRole.OWNER)
        manual = UserRole.objects.get(user=user)
        manual.revoke(reason="Revoked by an owner")

        user.details = {"roles": ["acme-owner"]}
        user.save()
        reconcile_autoprovisioned_roles(user)

        self.assertEqual(_active_roles(user), {CustomerRole.OWNER.name})
        manual.refresh_from_db()
        self.assertFalse(manual.is_active)
        self.assertEqual(manual.revoke_reason, "Revoked by an owner")
        issued = UserRole.objects.get(user=user, is_active=True)
        self.assertNotEqual(issued.pk, manual.pk)

    def test_losing_the_claim_keeps_the_role_by_default(self, _):
        """revoke_when_unmatched is off by default: enabling claim matching on a
        live deployment must not silently strip access."""
        self._rule(user_claims={"roles": ["acme-owner"]})
        user = _user(details={"roles": ["acme-owner"]})
        reconcile_autoprovisioned_roles(user)

        user.details = {"roles": []}
        user.save()
        reconcile_autoprovisioned_roles(user)

        self.assertEqual(_active_roles(user), {CustomerRole.OWNER.name})

    def test_hand_granted_role_is_never_revoked(self, _):
        """A grant made by a person carries no source and is untouchable, even
        when it names the same (user, scope, role) triple as a rule."""
        self._rule(user_claims={"roles": ["acme-owner"]}, revoke_when_unmatched=True)
        user = _user(details={"roles": ["other"]})
        self.customer.add_user(user, CustomerRole.OWNER)

        reconcile_autoprovisioned_roles(user)

        self.assertEqual(_active_roles(user), {CustomerRole.OWNER.name})
        self.assertEqual(UserRole.objects.get(user=user).source, "")

    def test_another_rules_grant_is_not_revoked(self, _):
        """Reconciliation only revokes rows issued by a rule that itself opted
        in — one rule must not clean up after another."""
        keeper = self._rule(
            name="keeper",
            user_claims={"roles": ["keep"]},
            customer_role=CustomerRole.SUPPORT,
        )
        self._rule(
            name="revoker",
            user_claims={"roles": ["gone"]},
            revoke_when_unmatched=True,
        )
        user = _user(details={"roles": ["keep"]})
        reconcile_autoprovisioned_roles(user)
        self.assertEqual(_active_roles(user), {CustomerRole.SUPPORT.name})

        reconcile_autoprovisioned_roles(user)

        self.assertEqual(_active_roles(user), {CustomerRole.SUPPORT.name})
        self.assertEqual(
            UserRole.objects.get(user=user, is_active=True).source,
            keeper.grant_source,
        )

    def test_concealed_role_is_skipped_without_aborting_the_rest(self, _):
        other_customer = structure_factories.CustomerFactory()
        self._rule(name="concealed", user_claims={"roles": ["x"]})
        self._rule(
            name="fine",
            customer=other_customer,
            customer_role=CustomerRole.SUPPORT,
            user_claims={"roles": ["x"]},
        )
        CustomerRoleConcealment.objects.create(
            role=CustomerRole.OWNER,
            content_type=ContentType.objects.get_for_model(type(self.customer)),
            object_id=self.customer.id,
        )
        user = _user(details={"roles": ["x"]})

        reconcile_autoprovisioned_roles(user)

        self.assertEqual(_active_roles(user), {CustomerRole.SUPPORT.name})

    def test_project_role_is_reconciled_for_an_existing_project(self, _):
        project = structure_factories.ProjectFactory(
            customer=self.customer, name="alice"
        )
        self._rule(
            create_project=True,
            customer_role=None,
            project_role=ProjectRole.ADMIN,
            project_name_template="{username}",
            user_claims={"roles": ["x"]},
        )
        user = _user(username="alice", details={"roles": ["x"]})

        reconcile_autoprovisioned_roles(user)

        self.assertTrue(project.has_user(user, ProjectRole.ADMIN))

    def test_dry_run_writes_nothing(self, _):
        user = _user(details={"roles": ["acme-owner"]})
        self._rule(user_claims={"roles": ["acme-owner"]})

        result = reconcile_autoprovisioned_roles(user, dry_run=True)

        self.assertEqual(len(result["granted"]), 1)
        self.assertEqual(_active_roles(user), set())

    def test_no_rules_is_cheap(self, _):
        """The common deployment has no rules at all; a login must not pay for
        the machinery."""
        user = _user()
        with self.assertNumQueries(1):
            reconcile_autoprovisioned_roles(user)

    def test_query_count_does_not_grow_with_rule_count(self, _):
        """Reconciliation runs on every login, so it must not issue a query per
        rule. Asserted as a comparison rather than an absolute count, which
        would be hostage to unrelated query-level changes."""
        self._rule(name="only", user_claims={"roles": ["r0"]})
        user = _user(details={"roles": ["r0"]})
        reconcile_autoprovisioned_roles(user)
        with CaptureQueriesContext(connection) as one_rule:
            reconcile_autoprovisioned_roles(user)

        for index in range(1, 6):
            self._rule(name=f"rule-{index}", user_claims={"roles": [f"r{index}"]})
        with CaptureQueriesContext(connection) as many_rules:
            reconcile_autoprovisioned_roles(user)

        self.assertEqual(len(many_rules), len(one_rule))

    @override_config(DEACTIVATE_USER_IF_NO_ROLES=True)
    def test_revoking_the_last_role_deactivates_the_user(self, _):
        """role_revoked has a dozen listeners; the one that changes the user's
        own state is worth pinning down. This is existing behaviour of the
        signal, reached for the first time by an automatic revocation."""
        self._rule(user_claims={"roles": ["acme-owner"]}, revoke_when_unmatched=True)
        user = _user(details={"roles": ["acme-owner"]})
        reconcile_autoprovisioned_roles(user)
        self.assertTrue(user.is_active)

        user.details = {"roles": []}
        user.save()
        reconcile_autoprovisioned_roles(user)

        user.refresh_from_db()
        self.assertFalse(user.is_active)
