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

echo "The Python client keeps every schema and endpoint."
