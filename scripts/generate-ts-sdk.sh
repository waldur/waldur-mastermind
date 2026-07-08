#!/bin/bash
set -e

# Get the script directory to reference other paths relative to mastermind root
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
MASTERMIND_DIR="$(dirname "$DIR")"
cd "$MASTERMIND_DIR"

echo "Generating TypeScript from schema..."
# Pin the typescript peer explicitly: @hey-api/openapi-ts@0.97.3 declares a loose
# peer range (">=5.5.3"), so an unpinned npx pulls whatever is newest. TypeScript
# 7.x (native rewrite) changed the JS API and breaks openapi-ts with
# "Cannot read properties of undefined (reading 'LineFeed')". Pin to a 5.x that
# matches the js-client consumer (typescript ^5.8.2).
npx --yes -p typescript@5.9.3 -p @hey-api/openapi-ts@0.97.3 openapi-ts -i waldur-typescript-schema.yaml

echo "Post-processing generated code..."
node scripts/patch-sdk.mjs

echo "TypeScript SDK regeneration and patching completed."
