# Software Catalog Test Data

This directory contains test fixtures for the unified software catalog system.

## Files

### EESSI Test Data

- `eessi_software_test.json` - Truncated EESSI main software data (3 packages)
- `eessi_extensions_python_test.json` - Truncated EESSI Python extensions (3 packages)
- `eessi_software_full.json` - Complete EESSI software catalog (for reference)
- `eessi_extensions_python_full.json` - Complete EESSI Python extensions (for reference)

### Spack Test Data

- `spack_repology_test.json` - Truncated Spack repology data (5 packages)
- `spack_repology_full.json` - Complete Spack repology data (for reference)

## Data Sources

**EESSI Data** (Downloaded: 2025-11-26):

- Source: <https://www.eessi.io/api_data/data/>
- Format: New multi-file API format
- Contains: Timestamp, architecture maps, software packages, extensions

**Spack Data** (Downloaded: 2025-11-26):

- Source: <https://raw.githubusercontent.com/spack/packages.spack.io/refs/heads/gh-pages/data/repology.json>
- Format: Repology-compatible JSON
- Contains: Package metadata, versions, dependencies, build info

## Usage in Tests

Test fixtures are used by:

- `test_catalog_loaders.py` - Unit tests for individual loaders
- `test_catalog_tasks.py` - Integration tests for Celery tasks

The truncated test files provide representative samples while keeping test execution fast.

## Updating Test Data

To refresh test data with latest snapshots:

```bash
# Update EESSI data
curl -s https://www.eessi.io/api_data/data/eessi_api_metadata_software.json -o eessi_software_full.json
curl -s https://www.eessi.io/api_data/data/eessi_api_metadata_ext-python.json -o eessi_extensions_python_full.json

# Update Spack data
curl -s https://raw.githubusercontent.com/spack/packages.spack.io/refs/heads/gh-pages/data/repology.json -o spack_repology_full.json

# Regenerate truncated test fixtures
jq '{timestamp: .timestamp, architectures_map: .architectures_map, gpu_architectures_map: .gpu_architectures_map, category_details: .category_details, software: (.software | to_entries | .[0:3] | from_entries)}' eessi_software_full.json > eessi_software_test.json

jq '{timestamp: .timestamp, architectures_map: .architectures_map, gpu_architectures_map: .gpu_architectures_map, category_details: .category_details, software: (.software | to_entries | .[0:3] | from_entries)}' eessi_extensions_python_full.json > eessi_extensions_python_test.json

jq '{last_update: .last_update, num_packages: (.packages | keys | length), packages: (.packages | to_entries | .[0:5] | from_entries)}' spack_repology_full.json > spack_repology_test.json
```
