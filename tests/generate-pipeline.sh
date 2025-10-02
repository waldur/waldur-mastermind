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

# The minimum number of discovered tests required to trigger parallel splitting.
# This prevents the overhead of splitting a tiny number of tests across many runners.
# For example, it's faster for one runner to execute 40 tests than for 10 runners
# to start up, coordinate, and each run 4 tests.
TEST_SPLITTING_THRESHOLD=300

# The absolute maximum number of parallel jobs to create. This acts as a ceiling
# to prevent creating an excessive number of jobs even for a very large test suite.
MAX_PARALLEL_JOBS=10

# The filename for the generated child pipeline configuration. This artifact is
# used by the `trigger` keyword in the main CI configuration.
PIPELINE_OUTPUT_FILE="generated-pipeline.yml"

# The filename for the variables that need to be passed from this "planning" stage
# to the downstream "execution" stage (the child pipeline).
VARS_OUTPUT_FILE="generated_vars.env"


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
  # --- STEP 3: DISCOVER TEST COUNT FOR PARTIAL RUNS ---
  # If it's not a full run, we need to determine the exact workload.
  echo "[+] STEP 2/4: Partial run detected."
  uv sync --extra dev

  echo "[+] Discovering number of tests for selected paths..."
  # Run pytest in "collect-only" mode. This is a dry run that finds all test
  # functions without executing them. We count the lines to get the total.
  # `|| true` prevents the script from failing if pytest encounters a collection error.
  TEST_COUNT=$(uv run pytest --collect-only -q ${SELECTED_PATHS} | wc -l || true)
  # Ensure TEST_COUNT is a valid integer, defaulting to 0 if the command failed.
  TEST_COUNT=${TEST_COUNT:-0}
  echo "[+] Discovered ${TEST_COUNT} tests."

  # Decide on splitting based on the discovered count and our threshold.
  if [ "${TEST_COUNT}" -ge "${TEST_SPLITTING_THRESHOLD}" ]; then
    echo "[+] Test count (${TEST_COUNT}) is >= threshold (${TEST_SPLITTING_THRESHOLD}). Enabling parallelization."
    ENABLE_SPLITTING_VAR="true"

    # Calculate the desired number of parallel jobs. The goal is to have roughly
    # TEST_SPLITTING_THRESHOLD tests per job. We use ceiling division to ensure
    # we have enough jobs for all tests.
    # Formula for shell integer ceiling division: (numerator + denominator - 1) / denominator
    PARALLEL_COUNT=$(( (TEST_COUNT + TEST_SPLITTING_THRESHOLD - 1) / TEST_SPLITTING_THRESHOLD ))

    # Cap the parallel count at the configured maximum.
    if [ "${PARALLEL_COUNT}" -gt "${MAX_PARALLEL_JOBS}" ]; then
      echo "[+] Calculated parallel count (${PARALLEL_COUNT}) exceeds maximum (${MAX_PARALLEL_JOBS}). Capping at ${MAX_PARALLEL_JOBS}."
      PARALLEL_COUNT=${MAX_PARALLEL_JOBS}
    fi
    echo "[+] Setting parallel job count to ${PARALLEL_COUNT}."

    # This variable will contain the `parallel: N` YAML keyword.
    PARALLEL_BLOCK="parallel: ${PARALLEL_COUNT}"
  else
    echo "[+] Test count (${TEST_COUNT}) is < threshold (${TEST_SPLITTING_THRESHOLD}). Disabling parallelization."
    ENABLE_SPLITTING_VAR="false"
    # This variable will be empty, so no parallel keyword is added to the YAML.
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
    reports:
      junit: report.xml
      coverage_report:
        coverage_format: cobertura
        path: coverage.xml

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
