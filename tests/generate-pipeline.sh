#!/usr/bin/env sh
#
# Dynamic GitLab CI Pipeline Generator for Waldur Unit Tests
#
# Purpose:
# This script is the "brain" of the dynamic testing pipeline. Its primary role is to
# generate a GitLab CI configuration file (`.yml`) for a child pipeline. This powerful
# pattern allows us to dynamically decide, based on the code changes in a merge
# request, whether to run tests in a single, fast job or to parallelize the test
# run across multiple runners for maximum speed.
#
# Workflow:
# 1. Select Paths: Runs a Python script to determine which application directories
#    are affected by the current code changes.
# 2. Early Exit & Optimization:
#    - If no tests are needed, it generates a "no-op" pipeline and exits.
#    - If a full test run ('src') is detected, it skips the slow test discovery
#      step and immediately decides to run in parallel.
# 3. Discover Test Count: For partial runs, it installs dependencies and uses
#    `pytest --collect-only` to get the *exact* number of tests to be executed.
# 4. Generate Pipeline: Based on the test count, it constructs a complete,
#    self-contained `generated-pipeline.yml` file that defines either a single
#    test job or a parallelized set of test jobs.
#
# This script is executed by the `generate_test_pipeline` job in the main `.gitlab-ci.yml`.

# Exit immediately if any command fails, preventing unexpected behavior.
set -e

# --- CONFIGURATION ---

# The minimum number of affected application paths required to trigger parallel
# splitting. Using app count instead of test count avoids the need for a full
# Django/database setup during the pipeline generation stage. With fewer than
# this many apps, the overhead of spinning up multiple runners outweighs the
# benefit of parallelization.
APP_SPLITTING_THRESHOLD=5

# The absolute maximum number of parallel jobs to create. This acts as a ceiling
# to prevent creating an excessive number of jobs even for a very large test suite.
MAX_PARALLEL_JOBS=15

# Known heavy apps and their weight multipliers (based on test file counts).
# Matched against the LAST path component (e.g. "marketplace" from
# "src/waldur_mastermind/marketplace"). Default weight for unlisted apps is 1.
HEAVY_APP_WEIGHTS="marketplace:8 openstack:4 structure:4 core:3 proposal:2"

# The filename for the generated child pipeline configuration. This artifact is
# used by the `trigger` keyword in the main CI configuration.
PIPELINE_OUTPUT_FILE="generated-pipeline.yml"

# The filename for the variables that need to be passed from this "planning" stage
# to the downstream "execution" stage (the child pipeline).
VARS_OUTPUT_FILE="generated_vars.env"


# --- FUNCTIONS ---

# Calculate a weighted score for the given space-separated list of app paths.
# Heavy apps (listed in HEAVY_APP_WEIGHTS) contribute more than 1 to the total,
# so a small number of heavy apps can still trigger parallelization.
calculate_weighted_score() {
  local paths="$1"
  local total=0
  for p in $paths; do
    local app_name
    app_name=$(basename "$p")
    local weight=1
    for entry in $HEAVY_APP_WEIGHTS; do
      local key="${entry%%:*}"
      local val="${entry##*:}"
      if [ "$app_name" = "$key" ]; then
        weight=$val
        break
      fi
    done
    total=$((total + weight))
  done
  echo "$total"
}

echo "--- Dynamic Pipeline Generator ---"

# --- STEP 1: SELECT AFFECTED APPLICATION PATHS ---
# This step determines which parts of the codebase *might* need testing based
# on the git diff and the project's dependency graph.
echo "[+] STEP 1/4: Selecting application paths based on Git changes..."
# Install dependency required for the selection script.
uv pip install PyYAML
# Execute the selection script and capture its space-separated output.
SELECTED_PATHS=$(uv run tests/select_tests.py)

# Create the dotenv artifact file. The `trigger` job in the parent pipeline
# will read this file and forward its variables to the child pipeline.
echo "TEST_PATHS=${SELECTED_PATHS}" > "${VARS_OUTPUT_FILE}"
echo "[+] Selected paths: '${SELECTED_PATHS}'"


# --- STEP 2: EARLY EXIT & "SRC" SHORTCUT ---
# This step handles the two simplest cases to avoid unnecessary work.

# Case 1: No tests selected.
# If the selection script returned an empty string, the changes (e.g., to a
# README file) don't require any backend tests.
if [ -z "${SELECTED_PATHS}" ]; then
  echo "[+] No tests selected. Generating a 'no-op' pipeline to report success."
  # We must generate a valid YAML file with at least one job for the child
  # pipeline to be considered valid by GitLab. This dummy job does nothing.
  cat > "${PIPELINE_OUTPUT_FILE}" <<-EOF
---
# This pipeline was generated because no tests were required for the changes.
noop_job:
  stage: test
  image: alpine:latest
  script:
    - echo "No relevant tests were selected to run for this merge request."
  rules:
  - if: \$CI_PIPELINE_SOURCE == "parent_pipeline"

EOF
  # Exit successfully with status 0.
  exit 0
fi

# Case 2: Full test suite selected.
# This is an optimization. If the selection script determines a full run is
# needed, it returns the simple string "src". In this case, we can be certain
# the test count is high and can skip the discovery step to save time.
if [ "${SELECTED_PATHS}" = "src" ]; then
  echo "[+] Full test suite ('src') was selected. Skipping test count discovery."
  echo "[+] Forcing parallelization with the maximum job count (${MAX_PARALLEL_JOBS})."
  ENABLE_SPLITTING_VAR="true"
  PARALLEL_BLOCK="parallel: ${MAX_PARALLEL_JOBS}"
else
  # --- STEP 3: DECIDE PARALLELIZATION FOR PARTIAL RUNS ---
  # We use the number of affected application paths as a proxy for workload.
  # This is fast, reliable, and avoids needing a full Django/database setup
  # that `pytest --collect-only` would require (the "Generate test pipeline"
  # job does not have a postgres service).
  echo "[+] STEP 2/4: Partial run detected."

  PATH_COUNT=$(echo "${SELECTED_PATHS}" | wc -w | tr -d ' ')
  WEIGHTED_SCORE=$(calculate_weighted_score "${SELECTED_PATHS}")
  echo "[+] Number of affected app paths: ${PATH_COUNT} (weighted score: ${WEIGHTED_SCORE})"

  if [ "${WEIGHTED_SCORE}" -ge "${APP_SPLITTING_THRESHOLD}" ]; then
    echo "[+] Weighted score (${WEIGHTED_SCORE}) >= threshold (${APP_SPLITTING_THRESHOLD}). Enabling parallelization."
    ENABLE_SPLITTING_VAR="true"

    # Use the weighted score directly as parallel count (1 weight-unit ≈ 1 job).
    # This gives enough parallelism for heavy apps like marketplace to spread
    # their slow tests across more runners.
    PARALLEL_COUNT=${WEIGHTED_SCORE}

    if [ "${PARALLEL_COUNT}" -gt "${MAX_PARALLEL_JOBS}" ]; then
      echo "[+] Calculated parallel count (${PARALLEL_COUNT}) exceeds maximum (${MAX_PARALLEL_JOBS}). Capping at ${MAX_PARALLEL_JOBS}."
      PARALLEL_COUNT=${MAX_PARALLEL_JOBS}
    fi
    echo "[+] Setting parallel job count to ${PARALLEL_COUNT}."

    PARALLEL_BLOCK="parallel: ${PARALLEL_COUNT}"
  else
    echo "[+] Weighted score (${WEIGHTED_SCORE}) < threshold (${APP_SPLITTING_THRESHOLD}). Running as single job."
    ENABLE_SPLITTING_VAR="false"
    PARALLEL_BLOCK=""
  fi
fi

# --- STEP 4: GENERATE THE SELF-CONTAINED PIPELINE YAML ---
# This is the final step where we construct the child pipeline's .yml file.
echo "[+] STEP 3/4: Generating child pipeline YAML configuration..."

# A safety check to ensure the Docker image name is passed from the parent CI job.
if [ -z "$WALDUR_MASTERMIND_TEST_IMAGE" ]; then
  echo "ERROR: Required environment variable WALDUR_MASTERMIND_TEST_IMAGE is not set."
  exit 1
fi

# The 'heredoc' (cat <<EOF) block below creates the `generated-pipeline.yml` file.
# It embeds shell variables like $WALDUR_MASTERMIND_TEST_IMAGE directly.
# The `services` block is hardcoded ("inlined") here for clarity and self-containment.
cat > "${PIPELINE_OUTPUT_FILE}" <<-EOF
---
# This file was dynamically generated by the 'generate-pipeline.sh' script.
# It is a self-contained pipeline configuration for running unit tests.

run_unit_tests:
  stage: test
  # The Docker image name is passed from the parent CI job's configuration.
  image: registry.hpc.ut.ee/mirror/\${WALDUR_MASTERMIND_TEST_IMAGE}
  interruptible: true

  # Auto-recover a shard that failed on infrastructure noise or a flaky test
  # instead of reding the whole sharded suite and the parent MR pipeline. A
  # single flaky shard was the dominant false-red source (~30% of MR pipeline
  # failures self-healed on an unchanged re-run). max=1 bounds the extra cost on
  # a genuinely broken shard to one rerun.
  retry:
    max: 1
    when:
      - runner_system_failure
      - stuck_or_timeout_failure
      - api_failure
      - script_failure

  # This rule ensures the job only runs when triggered as part of a child pipeline.
  # The '\$' is escaped to prevent expansion now and let GitLab expand it in the child pipeline.
  rules:
    - if: '\$CI_PIPELINE_SOURCE == "parent_pipeline"'

  # IMPORTANT: This service definition is hardcoded for the child pipeline.
  # It MUST be kept in sync with the 'services' block in the main .gitlab-ci.yml's
  # '.Unit test runner' template to ensure consistent test environments.
  services:
    - name: "registry.hpc.ut.ee/mirror/library/postgres:15-alpine"
      alias: postgres
      command:
        - "postgres"
        - "-cfsync=off"
        - "-cfull_page_writes=off"
        - "-cmax_connections=1000"
        - "-cshared_buffers=1GB"
        - "-ceffective_cache_size=4GB"
        - "-cwork_mem=32MB"
        - "-cmaintenance_work_mem=32MB"
        - "-ctemp_buffers=16MB"
        - "-cwal_buffers=48MB"

  variables:
    POSTGRES_DB: test_waldur
    POSTGRES_USER: runner
    POSTGRES_PASSWORD: waldur
    # This value was determined by the logic in this script.
    ENABLE_SPLITTING: "${ENABLE_SPLITTING_VAR}"
    UV_CACHE_DIR: .uv-cache
    UV_SYSTEM_PYTHON: 1
    UV_LINK_MODE: copy

  cache:
    - key:
        files:
          - uv.lock
      paths:
        - $UV_CACHE_DIR

  script:
    # We must wrap \$TEST_PATHS in double quotes.
    # This ensures that the entire space-separated string of paths is passed
    # as a SINGLE argument ($2) to the waldur-test script.
    # Inside waldur-test, the `eval` command will then correctly re-process
    # this string, performing the word-splitting that pytest requires.
    - tests/waldur-test \$ENABLE_SPLITTING \$TEST_PATHS

  artifacts:
    when: always
    # Sharded suite: this job fans out up to ${MAX_PARALLEL_JOBS} times per
    # pipeline, so it is the single largest producer of artifact rows in the
    # project. Without an explicit expire_in these inherit the instance default.
    expire_in: 1 week
    reports:
      junit: report.xml
      coverage_report:
        coverage_format: cobertura
        path: coverage.xml
    paths:
      - coverage.xml

  coverage: "/TOTAL.+ ([0-9]{1,3}%)/"
EOF

# Add the parallel block only if it's not empty
if [ -n "${PARALLEL_BLOCK}" ]; then
  echo "  ${PARALLEL_BLOCK}" >> "${PIPELINE_OUTPUT_FILE}"
fi

echo "-----------------------------------"
echo "[+] STEP 4/4: Final generated configuration:"
echo "--- Pipeline YAML ---"
cat "${PIPELINE_OUTPUT_FILE}"
echo "--- Variables ---"
cat "${VARS_OUTPUT_FILE}"
echo "-----------------------------------"
echo "Generator script finished successfully."
