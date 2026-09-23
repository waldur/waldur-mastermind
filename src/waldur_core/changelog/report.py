"""Upgrade report and announcement text built from pending changelog entries.

Both describe the whole upgrade, so they are built here from every pending
entry rather than in the browser from whichever page of the table is loaded.
"""

import datetime

ENTRY_TYPE_LABELS = {
    "breaking": "Breaking",
    "security": "Security",
    "deprecation": "Deprecation",
    "feature": "Feature",
    "improvement": "Improvement",
    "fix": "Fix",
}


def get_upgrade_commands(version):
    """Commands following the waldur-helm and waldur-docker-compose READMEs.

    Helm: the chart version equals the Waldur version, and the chart
    repository is added as `waldur-charts`. Docker Compose: the images are
    pinned in `.env`, so pulling alone would fetch the running version again;
    migrations then run on start-up in the `waldur-mastermind-db-migration`
    service, which the API waits for.
    """
    return {
        "helm": "\n".join(
            [
                "helm repo update waldur-charts",
                f"helm upgrade waldur waldur-charts/waldur --version {version} --reuse-values",
            ]
        ),
        "docker_compose": "\n".join(
            [
                *(
                    f"sed -i 's/^{name}=.*/{name}={version}/' .env"
                    for name in (
                        "WALDUR_MASTERMIND_IMAGE_TAG",
                        "WALDUR_HOMEPORT_IMAGE_TAG",
                    )
                ),
                "docker compose pull",
                "docker compose down",
                "docker compose up -d",
            ]
        ),
    }


def _plural(count, singular, plural):
    return f"{count} {singular if count == 1 else plural}"


def _actions(entries):
    return [action for entry in entries for action in entry.get("actions") or []]


def build_upgrade_report(current_version, target_version, entries, today=None):
    """Markdown upgrade brief: breaking and security changes, post-upgrade
    actions, every change, the upgrade commands and a pre-upgrade checklist."""
    today = today or datetime.date.today()
    breaking = [e for e in entries if e.get("type") == "breaking"]
    security = [e for e in entries if e.get("type") == "security"]
    actions = _actions(entries)
    commands = get_upgrade_commands(target_version)

    lines = [
        "# Waldur Upgrade Report",
        "",
        f"**From:** {current_version}",
        f"**To:** {target_version}",
        f"**Generated:** {today.isoformat()}",
        "",
    ]

    if breaking:
        lines += [f"## Breaking Changes ({len(breaking)})", ""]
        for entry in breaking:
            lines += [
                f"- **{entry.get('title')}** ({entry.get('category')})",
                f"  {entry.get('description')}",
                "",
            ]

    if security:
        lines += [f"## Security Fixes ({len(security)})", ""]
        for entry in security:
            detail = entry.get("security") or {}
            identifiers = "".join(
                f" ({detail[key]})" for key in ("ghsa", "cve") if detail.get(key)
            )
            lines.append(f"- **{entry.get('title')}**{identifiers}")
            lines.append(f"  Urgency: {detail.get('urgency') or 'unknown'}")
            if detail.get("mitigation"):
                lines.append(f"  Mitigation: {detail['mitigation']}")
            lines.append("")

    if actions:
        lines += [f"## Post-Upgrade Actions ({len(actions)})", ""]
        for action in actions:
            automatic = action.get("automatic")
            suffix = " *(automatic)*" if automatic else ""
            lines.append(
                f"- [{'x' if automatic else ' '}] {action.get('description')}{suffix}"
            )
        lines.append("")

    lines += [
        f"## All Changes ({len(entries)})",
        "",
        "| Type | Title | Category | Risk |",
        "|------|-------|----------|------|",
    ]
    for entry in entries:
        risk = (entry.get("impact") or {}).get("risk") or "-"
        lines.append(
            f"| {entry.get('type')} | {entry.get('title')} "
            f"| {entry.get('category')} | {risk} |"
        )
    lines.append("")

    lines += [
        "## Deployment Commands",
        "",
        "### Helm",
        "```bash",
        commands["helm"],
        "```",
        "",
        "### Docker Compose",
        "```bash",
        commands["docker_compose"],
        "```",
        "",
        "## Pre-Upgrade Checklist",
        "",
        "- [ ] Database backup completed",
        "- [ ] Disk space verified (2x database size)",
        "- [ ] Site-agent compatibility confirmed",
    ]
    if breaking:
        lines.append("- [ ] API consumers notified of breaking changes")
    if security:
        lines.append("- [ ] Users notified about security-related changes")
    lines.append("- [ ] Maintenance window announced")

    return "\n".join(lines) + "\n"


def build_announcement(current_version, target_version, entries):
    """Maintenance announcement text for the upgrade, and its type: a warning
    when the upgrade brings security fixes or breaking changes."""
    breaking = [e for e in entries if e.get("type") == "breaking"]
    security = [e for e in entries if e.get("type") == "security"]
    actions = _actions(entries)

    lines = [f"## Scheduled Upgrade: {current_version} → {target_version}", ""]

    if security:
        lines += [
            f"**{_plural(len(security), 'security fix', 'security fixes')}** included.",
            "",
        ]

    if breaking:
        lines.append(
            f"**{_plural(len(breaking), 'breaking change', 'breaking changes')}:**"
        )
        lines += [f"- {entry.get('title')}" for entry in breaking]
        lines.append("")

    counts = {}
    for entry in entries:
        label = ENTRY_TYPE_LABELS.get(entry.get("type"), entry.get("type"))
        counts[label] = counts.get(label, 0) + 1
    breakdown = ", ".join(f"{count} {label}" for label, count in counts.items())
    lines += [f"**{len(entries)} total changes:** {breakdown}", ""]

    if actions:
        lines.append("**Post-upgrade actions:**")
        for action in actions:
            suffix = " *(automatic)*" if action.get("automatic") else ""
            lines.append(f"- {action.get('description')}{suffix}")
        lines.append("")

    lines.append("Users may experience brief downtime during the migration step.")

    announcement_type = "warning" if security or breaking else "information"
    return "\n".join(lines), announcement_type
