#!/usr/bin/env bash
# Generate the Python client (waldur-api-client) from an OpenAPI schema with the
# same generator waldur/py-client's nightly uses, and fail if the generator
# drops a schema or an endpoint.
#
# openapi-python-client only warns when it cannot build a model: it removes the
# schema, types every response that refers to it as Any, and exits 0. That is
# how the client lost `Resource` from 8.1.4 on (a duplicate class name) and the
# provider `import_resource` endpoint (`ImportResourceRequest` had a
# `default: {}` on a nullable object), while every job stayed green.
#
# Usage: scripts/check_python_sdk_generation.sh waldur-openapi-schema.yaml
#   OPENAPI_PYTHON_CLIENT  pip requirement for the generator; defaults to the
#                          fork py-client installs. Needs `uvx` (pip install uv).
set -euo pipefail

schema=${1:?usage: $0 <openapi-schema.yaml>}
generator=${OPENAPI_PYTHON_CLIENT:-git+https://github.com/waldur/openapi-python-client.git}

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
# Only parsing matters here, so skip the default ruff post-hooks.
printf 'post_hooks: []\n' >"$workdir/config.yaml"

echo "Generating the Python client from $schema with $generator ..."
if ! uvx --quiet --from "$generator" openapi-python-client generate \
    --path "$schema" \
    --output-path "$workdir/client" \
    --overwrite \
    --meta none \
    --config "$workdir/config.yaml" >"$workdir/generate.log" 2>&1; then
    cat "$workdir/generate.log"
    echo "openapi-python-client failed." >&2
    exit 1
fi

if grep -Eq 'Unable to process schema|Endpoint will not be generated' "$workdir/generate.log"; then
    # Each message is followed by its reason and, for a schema, what was removed.
    {
        grep -A5 'Unable to process schema' "$workdir/generate.log" || true
        grep -A2 'Endpoint will not be generated' "$workdir/generate.log" || true
    } | grep -v -e '^$' -e '^--$' | cut -c1-300 >&2
    echo >&2
    echo "openapi-python-client dropped part of the API from the Python client (see above)." >&2
    echo "A name clash also shows as 'Invalid property in union'. Rename the serializer" >&2
    echo "involved, or fix the schema the generator cannot read." >&2
    exit 1
fi

# A GET endpoint whose 200 response declares no body still generates, but
# without sync()/asyncio(): sync_detailed().parsed is always None, and the
# client cannot read what the endpoint returns. Only these are expected to have
# no typed body: login redirects, a fire-and-forget trigger, and PDF downloads
# (the generator does not type application/pdf; read sync_detailed().content).
untyped_get_allowed='
api_auth_default_init_retrieve
api_auth_eduteams_complete_retrieve
api_auth_eduteams_init_retrieve
api_auth_keycloak_complete_retrieve
api_auth_keycloak_init_retrieve
api_auth_saml2_logout_retrieve
api_auth_tara_complete_retrieve
api_auth_tara_init_retrieve
hpc_workspaces_jobs_performance_pdf_retrieve
proposal_proposals_pdf_retrieve
proposal_protected_calls_workflow_report_retrieve
sync_issues_retrieve
'
# Module names of the GET endpoints, and of the endpoints that got a sync().
# Both lists come from one grep over the tree each, not from xargs: a grep that
# finds nothing exits 1, which under pipefail would end the script unexplained.
module_names() { sed 's|.*/||; s|\.py$||' | sort; }
get_modules=$(grep -rl --include='*.py' '"method": "get"' "$workdir/client/api" | module_names) ||
    { echo "Found no GET endpoints in the generated client." >&2; exit 1; }
typed_modules=$(grep -rl --include='*.py' '^def sync(' "$workdir/client/api" | module_names) ||
    { echo "Found no endpoint with a sync() in the generated client." >&2; exit 1; }
untyped_get=$(comm -23 <(printf '%s\n' "$get_modules") <(printf '%s\n' "$typed_modules"))
allowed=$(printf '%s' "$untyped_get_allowed" | sed '/^$/d' | sort)

unexpected=$(comm -23 <(printf '%s\n' "$untyped_get") <(printf '%s\n' "$allowed"))
if [ -n "$unexpected" ]; then
    printf '%s\n' "$unexpected" >&2
    echo >&2
    echo "These GET endpoints declare no response body, so the Python client returns" >&2
    echo "None for them. Declare the response with @extend_schema(responses=...)." >&2
    exit 1
fi

stale=$(comm -13 <(printf '%s\n' "$untyped_get") <(printf '%s\n' "$allowed"))
if [ -n "$stale" ]; then
    printf '%s\n' "$stale" >&2
    echo >&2
    echo "These endpoints are allowed to have no typed body but now have one or are" >&2
    echo "gone. Remove them from untyped_get_allowed in $0." >&2
    exit 1
fi

echo "The Python client keeps every schema and endpoint."
