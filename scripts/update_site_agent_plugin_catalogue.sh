#!/usr/bin/env bash
# Refresh the vendored site agent plugin catalogue
# (src/waldur_mastermind/marketplace/plugin_catalogue/catalogue.json).
#
#   scripts/update_site_agent_plugin_catalogue.sh              # from the site agent's main CI artifact
#   scripts/update_site_agent_plugin_catalogue.sh <ref>        # from a branch or tag's CI artifact
#   scripts/update_site_agent_plugin_catalogue.sh --from DIR   # built from a local waldur-site-agent checkout
#
# Add --check (first) to compare instead of write: it prints what differs and
# exits 1 when the vendored catalogue is out of date. CI runs it that way
# against a fresh clone of the site agent (job "Check site agent plugin
# catalogue").
set -euo pipefail

check=false
if [ "${1:-}" = "--check" ]; then
    check=true
    shift
fi

target="$(cd "$(dirname "$0")/.." && pwd)/src/waldur_mastermind/marketplace/plugin_catalogue/catalogue.json"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

if [ "${1:-}" = "--from" ]; then
    checkout="${2:?--from needs a waldur-site-agent checkout}"
    (cd "$checkout" && uv run --all-packages waldur_site_export_plugin_catalogue --output "$tmp")
else
    ref="${1:-main}"
    project="waldur%2Fwaldur-site-agent"
    # The latest successful export job on the ref, rather than
    # jobs/artifacts/<ref>/raw, which waits for the whole pipeline (E2E jobs
    # included) to succeed.
    job=$(glab api "projects/${project}/jobs?scope[]=success&per_page=100" |
        python3 -c 'import json, sys; ref = sys.argv[1]; jobs = [j for j in json.load(sys.stdin) if j["name"] == "Export plugin catalogue" and j["ref"] == ref]; print(jobs[0]["id"] if jobs else "")' "$ref")
    if [ -z "$job" ]; then
        echo "No successful 'Export plugin catalogue' job on ${ref} among the latest jobs" >&2
        exit 1
    fi
    echo "From job ${job} on ${ref}"
    glab api "projects/${project}/jobs/${job}/artifacts/plugin-catalogue.json" >"$tmp"
fi

python3 -c 'import json, sys; c = json.load(open(sys.argv[1])); assert c["catalogue_version"] == 1, c["catalogue_version"]; print("site agent", c["agent_version"], "-", len(c["plugins"]), "plugins")' "$tmp"

if $check; then
    python3 - "$target" "$tmp" <<'PY'
import json
import sys

vendored, current = (json.load(open(path)) for path in sys.argv[1:3])


def content(catalogue):
    """The catalogue without its version stamps: a site agent build that only
    bumps a version changes nothing Waldur validates against."""
    return {
        **{key: value for key, value in catalogue.items() if key != "agent_version"},
        "plugins": [
            {key: value for key, value in plugin.items() if key != "version"}
            for plugin in catalogue["plugins"]
        ],
    }


if content(vendored) == content(current):
    if vendored != current:
        print(
            "The vendored plugin catalogue is up to date; only versions differ "
            f"(site agent {vendored['agent_version']} -> {current['agent_version']})."
        )
    else:
        print("The vendored plugin catalogue is up to date.")
    sys.exit(0)

old = {plugin["name"]: plugin for plugin in vendored["plugins"]}
new = {plugin["name"]: plugin for plugin in current["plugins"]}
lines = []
if vendored["agent_version"] != current["agent_version"]:
    lines.append(f"agent version: {vendored['agent_version']} -> {current['agent_version']}")
if vendored.get("offering_secret_fields") != current.get("offering_secret_fields"):
    lines.append("offering secret fields changed")
for name in sorted(new.keys() - old.keys()):
    lines.append(f"{name}: new plugin, roles {new[name]['roles']}")
for name in sorted(old.keys() - new.keys()):
    lines.append(f"{name}: removed")
for name in sorted(old.keys() & new.keys()):
    before, after = old[name], new[name]
    for field in ("roles", "secret_settings", "sensitive_settings"):
        if before.get(field) != after.get(field):
            lines.append(f"{name}: {field} {before.get(field)} -> {after.get(field)}")
    for field in ("settings_schema", "component_schema"):
        if before.get(field) != after.get(field):
            lines.append(f"{name}: {field} changed")
    if before.get("version") != after.get("version"):
        lines.append(f"{name}: version {before.get('version')} -> {after.get('version')}")

print("The vendored plugin catalogue differs from the site agent's:")
for line in lines:
    print(f"  - {line}")
print("Refresh it with scripts/update_site_agent_plugin_catalogue.sh and commit.")
sys.exit(1)
PY
    exit $?
fi

mv "$tmp" "$target"
trap - EXIT
echo "Wrote $target"
