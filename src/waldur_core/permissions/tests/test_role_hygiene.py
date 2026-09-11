import json
from io import StringIO

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.permissions import hygiene
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.models import Role, RoleAvailability
from waldur_core.permissions.serializers import clone_role_for_customer
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures


def content_type(app_label, model):
    return ContentType.objects.get_by_natural_key(app_label, model)


def checks_for(role_name):
    return {
        finding.check
        for finding in hygiene.collect_findings()
        if finding.role_name == role_name
    }


def finding_for(role_name, check):
    return next(
        finding
        for finding in hygiene.collect_findings()
        if finding.role_name == role_name and finding.check == check
    )


def bind(role, customer):
    return RoleAvailability.objects.create(
        role=role,
        content_type=ContentType.objects.get_for_model(Customer),
        object_id=customer.id,
    )


class RoleNameCheckTest(test.APITestCase):
    def test_free_form_name_is_not_a_code(self):
        # The exact shape seen in the field: a human sentence in the name column.
        Role.objects.create(
            name="Researcher (project member)",
            description="Researcher",
            content_type=content_type("structure", "project"),
        )
        self.assertIn("name-not-a-code", checks_for("Researcher (project member)"))

    def test_machine_code_is_accepted(self):
        Role.objects.create(
            name="PROJECT.RESEARCHER",
            description="Researcher",
            content_type=content_type("structure", "project"),
        )
        self.assertNotIn("name-not-a-code", checks_for("PROJECT.RESEARCHER"))

    def test_clone_name_with_owner_slug_is_a_code(self):
        customer = structure_factories.CustomerFactory(slug="acme-ltd")
        clone = clone_role_for_customer(CustomerRole.OWNER, customer, "Owner")
        self.assertNotIn("name-not-a-code", checks_for(clone.name))

    def test_scope_prefix_mismatch_is_reported(self):
        Role.objects.create(
            name="PROJECT.REVIEWER",
            description="Reviewer",
            content_type=content_type("structure", "customer"),
        )
        self.assertIn("scope-prefix-mismatch", checks_for("PROJECT.REVIEWER"))

    def test_non_scope_prefix_makes_no_scope_claim(self):
        Role.objects.create(
            name="RESEARCHER.LEAD",
            description="Lead researcher",
            content_type=content_type("structure", "project"),
        )
        self.assertNotIn("scope-prefix-mismatch", checks_for("RESEARCHER.LEAD"))

    def test_system_role_on_its_canonical_scope_is_not_a_prefix_mismatch(self):
        # CUSTOMER.MANAGER lives on ServiceProvider by design.
        role = Role.objects.get_system_role(
            "CUSTOMER.MANAGER", content_type("marketplace", "serviceprovider")
        )
        self.assertNotIn("scope-prefix-mismatch", checks_for(role.name))


class SystemRoleCheckTest(test.APITestCase):
    def test_unknown_system_role_name_is_reported(self):
        # What project.add_user(user, "Some label") leaves behind.
        Role.objects.create(
            name="PROJECT.RESEARCHER",
            description="Researcher",
            is_system_role=True,
            content_type=content_type("structure", "project"),
        )
        self.assertIn("system-name-unknown", checks_for("PROJECT.RESEARCHER"))

    def test_known_system_role_is_not_reported(self):
        self.assertNotIn("system-name-unknown", checks_for(ProjectRole.ADMIN.name))

    def test_shipped_role_without_a_role_enum_member_is_not_reported(self):
        # CUSTOMER.CALL_ORGANIZER ships in permissions.yaml with a migration of
        # its own but has no RoleEnum member, so it is only known to the
        # checker through SYSTEM_ROLE_SCOPES.
        role = Role.objects.get_system_role(
            "CUSTOMER.CALL_ORGANIZER",
            content_type("proposal", "callmanagingorganisation"),
        )
        checks = checks_for(role.name)
        self.assertNotIn("system-name-unknown", checks)
        self.assertNotIn("scope-prefix-mismatch", checks)

    def test_unknown_system_role_name_is_a_warning(self):
        # import_roles marks every role in a deployment's own permissions.yaml
        # as a system role, and that deployment cannot extend
        # SYSTEM_ROLE_SCOPES — so this must not fail an ops check.
        Role.objects.create(
            name="PROJECT.RESEARCHER",
            description="Researcher",
            is_system_role=True,
            content_type=content_type("structure", "project"),
        )
        finding = finding_for("PROJECT.RESEARCHER", "system-name-unknown")
        self.assertEqual(finding.severity, hygiene.WARNING)

    def test_system_role_on_wrong_scope_is_reported(self):
        Role.objects.create(
            name="CUSTOMER.OWNER",
            description="Owner",
            is_system_role=True,
            content_type=content_type("structure", "project"),
        )
        checks = checks_for("CUSTOMER.OWNER")
        self.assertIn("system-scope-mismatch", checks)

    def test_custom_role_is_not_checked_against_system_names(self):
        Role.objects.create(
            name="PROJECT.RESEARCHER",
            description="Researcher",
            content_type=content_type("structure", "project"),
        )
        self.assertNotIn("system-name-unknown", checks_for("PROJECT.RESEARCHER"))


class RoleBindingCheckTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory(slug="acme-ltd")

    def test_clone_is_clean(self):
        clone = clone_role_for_customer(CustomerRole.OWNER, self.customer, "Owner")
        self.assertEqual(checks_for(clone.name), set())

    def test_clone_without_availability_is_reported(self):
        clone = clone_role_for_customer(CustomerRole.OWNER, self.customer, "Owner")
        RoleAvailability.objects.filter(role=clone).delete()
        self.assertIn("template-without-scope", checks_for(clone.name))

    def test_clone_name_drift_is_reported(self):
        clone = clone_role_for_customer(CustomerRole.OWNER, self.customer, "Owner")
        clone.name = "CUSTOMER.old-slug.OWNER"
        clone.save(update_fields=["name"])
        finding = finding_for("CUSTOMER.old-slug.OWNER", "clone-name-drift")
        self.assertEqual(finding.details["expected_name"], "CUSTOMER.acme-ltd.OWNER")

    def test_collision_suffix_is_not_drift(self):
        clone = clone_role_for_customer(CustomerRole.OWNER, self.customer, "Owner")
        clone.name = f"{clone.name}-2"
        clone.save(update_fields=["name"])
        self.assertNotIn("clone-name-drift", checks_for(clone.name))

    def test_role_bound_to_two_organizations_is_reported(self):
        clone = clone_role_for_customer(CustomerRole.OWNER, self.customer, "Owner")
        bind(clone, structure_factories.CustomerFactory())
        self.assertIn("multi-org-binding", checks_for(clone.name))

    def test_org_private_role_without_template_is_reported(self):
        role = Role.objects.create(
            name="CUSTOMER.RESEARCHER",
            description="Researcher",
            content_type=content_type("structure", "customer"),
        )
        bind(role, self.customer)
        self.assertIn("org-role-unmanaged", checks_for("CUSTOMER.RESEARCHER"))

    def test_unbound_custom_role_is_reported_as_global(self):
        Role.objects.create(
            name="PROJECT.RESEARCHER",
            description="Researcher",
            content_type=content_type("structure", "project"),
        )
        self.assertIn("global-custom-role", checks_for("PROJECT.RESEARCHER"))

    def test_unbound_system_role_is_not_reported_as_global(self):
        self.assertNotIn("global-custom-role", checks_for(ProjectRole.ADMIN.name))

    def test_global_role_carries_the_number_of_organizations_holding_it(self):
        role = Role.objects.create(
            name="PROJECT.RESEARCHER",
            description="Researcher",
            content_type=content_type("structure", "project"),
        )
        first = structure_fixtures.ProjectFixture()
        second = structure_fixtures.ProjectFixture()
        other_project_same_org = structure_factories.ProjectFactory(
            customer=first.customer
        )
        for scope in (first.project, second.project, other_project_same_org):
            scope.add_user(structure_factories.UserFactory(), role)

        finding = finding_for("PROJECT.RESEARCHER", "global-custom-role")
        self.assertEqual(finding.details["organization_count"], 2)
        self.assertEqual(finding.details["assignment_count"], 3)


class PermissionScopeCheckTest(test.APITestCase):
    def test_permission_outside_the_scope_is_reported(self):
        role = Role.objects.create(
            name="PROJECT.RESEARCHER",
            description="Researcher",
            content_type=content_type("structure", "project"),
        )
        role.add_permission(PermissionEnum.CLOSE_ROUNDS)
        finding = finding_for("PROJECT.RESEARCHER", "cross-scope-permission")
        self.assertEqual(finding.details["permissions"], ["CALL.CLOSE_ROUNDS"])

    def test_permission_on_a_scope_below_the_role_is_not_reported(self):
        # A customer owner legitimately manages the organization's projects.
        role = Role.objects.create(
            name="CUSTOMER.RESEARCHER",
            description="Researcher",
            content_type=content_type("structure", "customer"),
        )
        role.add_permission(PermissionEnum.UPDATE_PROJECT)
        self.assertNotIn("cross-scope-permission", checks_for("CUSTOMER.RESEARCHER"))

    def test_permission_on_a_scope_above_the_role_is_reported(self):
        role = Role.objects.create(
            name="PROJECT.RESEARCHER",
            description="Researcher",
            content_type=content_type("structure", "project"),
        )
        role.add_permission(PermissionEnum.UPDATE_CUSTOMER)
        self.assertIn("cross-scope-permission", checks_for("PROJECT.RESEARCHER"))

    def test_token_only_permission_is_inert_on_any_role(self):
        role = Role.objects.create(
            name="PROJECT.RESEARCHER",
            description="Researcher",
            content_type=content_type("structure", "project"),
        )
        role.add_permission(PermissionEnum.STAFF_ACCESS)
        self.assertIn("cross-scope-permission", checks_for("PROJECT.RESEARCHER"))


class RoleLabelCheckTest(test.APITestCase):
    def test_missing_description_is_reported(self):
        Role.objects.create(
            name="PROJECT.RESEARCHER",
            content_type=content_type("structure", "project"),
        )
        self.assertIn("label-missing", checks_for("PROJECT.RESEARCHER"))

    def test_description_repeating_the_name_is_reported(self):
        Role.objects.create(
            name="PROJECT.RESEARCHER",
            description="PROJECT.RESEARCHER",
            content_type=content_type("structure", "project"),
        )
        self.assertIn("label-equals-name", checks_for("PROJECT.RESEARCHER"))


class CatalogRoleExemptionTest(test.APITestCase):
    def test_offering_catalog_roles_are_exempt(self):
        # Provider-chosen names, duplicates allowed by design.
        Role.objects.create(
            name="Cluster admin",
            content_type=content_type("marketplace", "resource"),
        )
        self.assertEqual(checks_for("Cluster admin"), set())


class RoleHygieneReportTest(test.APITestCase):
    def test_report_counts_findings_by_severity(self):
        Role.objects.create(
            name="Researcher (project member)",
            content_type=content_type("structure", "project"),
        )
        report = hygiene.build_report()
        self.assertGreaterEqual(report["error_count"], 1)
        self.assertGreaterEqual(report["info_count"], 1)
        self.assertGreaterEqual(report["roles_with_findings"], 1)

    def test_findings_are_ordered_most_severe_first(self):
        Role.objects.create(
            name="Researcher (project member)",
            content_type=content_type("structure", "project"),
        )
        severities = [
            finding["severity"] for finding in hygiene.build_report()["findings"]
        ]
        self.assertEqual(
            severities, sorted(severities, key=hygiene.SEVERITY_ORDER.index)
        )

    def test_catalog_roles_are_not_counted_as_checked(self):
        Role.objects.create(
            name="Cluster admin",
            content_type=content_type("marketplace", "resource"),
        )
        before = hygiene.build_report()["roles_checked"]
        Role.objects.create(
            name="Cluster operator",
            content_type=content_type("marketplace", "resource"),
        )
        self.assertEqual(hygiene.build_report()["roles_checked"], before)


class CheckRoleNamesCommandTest(test.APITestCase):
    def call(self, *args):
        out = StringIO()
        call_command("check_role_names", *args, stdout=out)
        return out.getvalue()

    def test_check_matching_nothing_reports_nothing(self):
        output = self.call("--check", "multi-org-binding", "--exit-zero")
        self.assertIn("nothing to report", output)

    def test_malformed_role_is_reported(self):
        Role.objects.create(
            name="Researcher (project member)",
            description="Researcher",
            content_type=content_type("structure", "project"),
        )
        output = self.call("--exit-zero")
        self.assertIn("Researcher (project member)", output)
        self.assertIn("name-not-a-code", output)

    def test_errors_make_the_command_exit_non_zero(self):
        Role.objects.create(
            name="Researcher (project member)",
            description="Researcher",
            content_type=content_type("structure", "project"),
        )
        with self.assertRaises(SystemExit) as context:
            self.call()
        self.assertEqual(context.exception.code, 1)

    def test_warnings_alone_do_not_fail_the_command(self):
        Role.objects.create(
            name="PROJECT.RESEARCHER",
            description="Researcher",
            content_type=content_type("structure", "project"),
        )
        self.call("--severity", "warning")

    def test_a_deployments_own_system_role_does_not_fail_the_command(self):
        Role.objects.create(
            name="PROJECT.RESEARCHER",
            description="Researcher",
            is_system_role=True,
            content_type=content_type("structure", "project"),
        )
        output = self.call()
        self.assertIn("system-name-unknown", output)

    def test_severity_filter_hides_lower_severities(self):
        Role.objects.create(
            name="PROJECT.RESEARCHER",
            content_type=content_type("structure", "project"),
        )
        output = self.call("--severity", "warning")
        self.assertIn("global-custom-role", output)
        self.assertNotIn("label-missing", output)

    def test_check_filter_keeps_only_the_named_check(self):
        Role.objects.create(
            name="PROJECT.RESEARCHER",
            content_type=content_type("structure", "project"),
        )
        output = self.call("--check", "label-missing")
        self.assertIn("label-missing", output)
        self.assertNotIn("global-custom-role", output)

    def test_json_output_is_machine_readable(self):
        Role.objects.create(
            name="PROJECT.RESEARCHER",
            content_type=content_type("structure", "project"),
        )
        report = json.loads(self.call("--format", "json", "--exit-zero"))
        self.assertIn("findings", report)
        self.assertTrue(
            any(
                finding["role_name"] == "PROJECT.RESEARCHER"
                for finding in report["findings"]
            )
        )


class RoleHygieneEndpointTest(test.APITestCase):
    def setUp(self):
        self.url = reverse("role-hygiene-report")
        Role.objects.create(
            name="Researcher (project member)",
            content_type=content_type("structure", "project"),
        )

    def test_staff_can_read_the_report(self):
        self.client.force_authenticate(structure_factories.UserFactory(is_staff=True))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["error_count"], 1)
        self.assertTrue(
            any(
                finding["role_name"] == "Researcher (project member)"
                for finding in response.data["findings"]
            )
        )

    def test_owner_cannot_read_the_report(self):
        fixture = structure_fixtures.CustomerFixture()
        self.client.force_authenticate(fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_inactive_staff_cannot_read_the_report(self):
        self.client.force_authenticate(
            structure_factories.UserFactory(is_staff=True, is_active=False)
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_user_cannot_read_the_report(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
