import json

from django.core.management.base import BaseCommand

from waldur_core.permissions import hygiene


class Command(BaseCommand):
    help = """
    Report malformed, mis-scoped and silently global roles.

    Read-only: nothing is created, changed or deleted. Exits with status 1 when
    an error-severity finding is reported, so it can run as an ops check. The
    status follows what the filters actually report.

    Usage:
        waldur check_role_names
        waldur check_role_names --severity warning
        waldur check_role_names --format json
        waldur check_role_names --check global-custom-role --check org-role-unmanaged
    """

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--format",
            choices=["text", "json"],
            default="text",
            help="Output format (default: text)",
        )
        parser.add_argument(
            "--severity",
            choices=list(hygiene.SEVERITY_ORDER),
            default=hygiene.INFO,
            help="Lowest severity to report (default: info, i.e. everything)",
        )
        parser.add_argument(
            "--check",
            action="append",
            choices=sorted(hygiene.CHECK_SEVERITIES),
            dest="checks",
            help="Report only this check; repeat for several",
        )
        parser.add_argument(
            "--exit-zero",
            action="store_true",
            help="Always exit with status 0, even when errors are reported",
        )

    def handle(self, *args, **options):
        report = hygiene.build_report()
        findings = self._filter(
            report["findings"], options["severity"], options.get("checks")
        )

        if options["format"] == "json":
            self.stdout.write(
                json.dumps({**report, "findings": findings}, indent=2, default=str)
            )
        else:
            self._write_text(report, findings)

        has_errors = any(finding["severity"] == hygiene.ERROR for finding in findings)
        if has_errors and not options["exit_zero"]:
            raise SystemExit(1)

    def _filter(self, findings, severity, checks):
        cutoff = hygiene.SEVERITY_ORDER.index(severity)
        return [
            finding
            for finding in findings
            if hygiene.SEVERITY_ORDER.index(finding["severity"]) <= cutoff
            and (not checks or finding["check"] in checks)
        ]

    def _write_text(self, report, findings):
        styles = {
            hygiene.ERROR: self.style.ERROR,
            hygiene.WARNING: self.style.WARNING,
            hygiene.INFO: self.style.NOTICE,
        }
        if not findings:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Checked {report['roles_checked']} roles, nothing to report."
                )
            )
            return

        current_severity = None
        for finding in findings:
            if finding["severity"] != current_severity:
                current_severity = finding["severity"]
                self.stdout.write("")
                self.stdout.write(styles[current_severity](current_severity.upper()))
            label = finding["role_description"] or "(no description)"
            self.stdout.write(f"  {finding['role_name']} — {label}")
            self.stdout.write(f"    {finding['check']}: {finding['message']}")

        self.stdout.write("")
        self.stdout.write(
            f"Checked {report['roles_checked']} roles: "
            f"{report['error_count']} error(s), "
            f"{report['warning_count']} warning(s), "
            f"{report['info_count']} info, "
            f"across {report['roles_with_findings']} role(s)."
        )
