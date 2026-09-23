import datetime

from django.test import SimpleTestCase

from waldur_core.changelog.report import (
    build_announcement,
    build_upgrade_report,
    get_upgrade_commands,
)


def _entry(**overrides):
    entry = {
        "id": "8.1.3-1",
        "type": "fix",
        "category": "marketplace",
        "title": "A fix",
        "description": "Fixed something.",
        "impact": {"risk": "low"},
    }
    entry.update(overrides)
    return entry


ENTRIES = [
    _entry(id="1", type="breaking", title="Legacy endpoint removed"),
    _entry(
        id="2",
        type="security",
        title="Sessions rotated",
        security={"urgency": "high", "cve": "CVE-2026-1", "mitigation": "Log out."},
        actions=[{"description": "Run waldur migrate", "automatic": True}],
    ),
    _entry(id="3", actions=[{"description": "Enable X", "automatic": False}]),
]


class UpgradeReportTest(SimpleTestCase):
    def setUp(self):
        self.report = build_upgrade_report(
            "8.1.2", "8.1.3", ENTRIES, today=datetime.date(2026, 9, 23)
        )

    def test_sections(self):
        headings = [line for line in self.report.splitlines() if line.startswith("#")]
        self.assertEqual(
            headings,
            [
                "# Waldur Upgrade Report",
                "## Breaking Changes (1)",
                "## Security Fixes (1)",
                "## Post-Upgrade Actions (2)",
                "## All Changes (3)",
                "## Deployment Commands",
                "### Helm",
                "### Docker Compose",
                "## Pre-Upgrade Checklist",
            ],
        )

    def test_security_details_and_actions(self):
        self.assertIn("- **Sessions rotated** (CVE-2026-1)", self.report)
        self.assertIn("  Mitigation: Log out.", self.report)
        self.assertIn("- [x] Run waldur migrate *(automatic)*", self.report)
        self.assertIn("- [ ] Enable X", self.report)
        self.assertIn("--version 8.1.3 --reuse-values", self.report)
        self.assertIn("**Generated:** 2026-09-23", self.report)

    def test_no_breaking_or_security_sections_when_absent(self):
        report = build_upgrade_report("8.1.2", "8.1.3", [_entry()])
        self.assertNotIn("## Breaking Changes", report)
        self.assertNotIn("## Security Fixes", report)
        self.assertNotIn("API consumers notified", report)


class AnnouncementTest(SimpleTestCase):
    def test_warning_with_breaking_and_security(self):
        text, announcement_type = build_announcement("8.1.2", "8.1.3", ENTRIES)
        self.assertEqual(announcement_type, "warning")
        self.assertIn("## Scheduled Upgrade: 8.1.2 → 8.1.3", text)
        self.assertIn("**1 security fix** included.", text)
        self.assertIn("**1 breaking change:**\n- Legacy endpoint removed", text)
        self.assertIn("**3 total changes:** 1 Breaking, 1 Security, 1 Fix", text)
        self.assertIn("- Run waldur migrate *(automatic)*", text)

    def test_information_without_them(self):
        text, announcement_type = build_announcement("8.1.2", "8.1.3", [_entry()])
        self.assertEqual(announcement_type, "information")
        self.assertIn("**1 total changes:** 1 Fix", text)


class UpgradeCommandsTest(SimpleTestCase):
    def test_helm_uses_the_documented_repository_alias(self):
        helm = get_upgrade_commands("8.1.3")["helm"].splitlines()
        self.assertEqual(
            helm,
            [
                "helm repo update waldur-charts",
                "helm upgrade waldur waldur-charts/waldur --version 8.1.3 --reuse-values",
            ],
        )

    def test_docker_compose_pins_the_new_images_and_leaves_migrations_to_startup(
        self,
    ):
        compose = get_upgrade_commands("8.1.3")["docker_compose"].splitlines()
        self.assertEqual(
            compose[:2],
            [
                "sed -i 's/^WALDUR_MASTERMIND_IMAGE_TAG=.*/"
                "WALDUR_MASTERMIND_IMAGE_TAG=8.1.3/' .env",
                "sed -i 's/^WALDUR_HOMEPORT_IMAGE_TAG=.*/"
                "WALDUR_HOMEPORT_IMAGE_TAG=8.1.3/' .env",
            ],
        )
        self.assertEqual(
            compose[2:],
            ["docker compose pull", "docker compose down", "docker compose up -d"],
        )
        self.assertNotIn("migrate", "\n".join(compose))
