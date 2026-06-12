#!/bin/bash
set -e

# Get the script directory to reference other paths relative to mastermind root
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
MASTERMIND_DIR="$(dirname "$DIR")"
cd "$MASTERMIND_DIR"

echo "Generating TypeScript from schema..."
npx --yes @hey-api/openapi-ts@0.97.3 -i waldur-typescript-schema.yaml

echo "Post-processing generated code..."
node scripts/patch-sdk.mjs

echo "TypeScript SDK regeneration and patching completed."
