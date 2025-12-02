# Marketplace Software Catalogs

This guide covers the software catalog system in Waldur's marketplace, including support for EESSI (European Environment for Scientific Software Installations), Spack, and other software catalogs.

## Overview

The software catalog system allows marketplace offerings to expose large collections of scientific and HPC software packages from external catalogs. Instead of manually tracking individual software installations, offerings can reference comprehensive software catalogs with thousands of packages. Waldur supports multiple catalog sources including:

- **EESSI**: Binary runtime environment with pre-compiled HPC software
- **Spack**: Source-based package manager for scientific computing
- **Future support**: conda-forge, modules, and custom catalogs

## Architecture

### Unified Catalog Loader Framework

Waldur uses a unified catalog loader framework that provides:

- **BaseCatalogLoader**: Abstract base class for all catalog loaders
- **EESSICatalogLoader**: Loader for EESSI catalogs from new API format
- **SpackCatalogLoader**: Loader for Spack catalogs from repology.json format
- **Extensible design**: Support for additional catalog types

### Data Models

The system uses relational models for efficient storage and querying:

- **SoftwareCatalog**: Represents a software catalog (e.g., EESSI 2023.06, Spack 2024.12)
- **SoftwarePackage**: Individual software packages within catalogs
- **SoftwareVersion**: Specific versions of packages
- **SoftwareTarget**: Architecture/platform-specific installations or build variants
- **OfferingSoftwareCatalog**: Links offerings to available catalogs

### Catalog Types

- **binary_runtime**: Pre-compiled software ready to use (EESSI)
- **source_package**: Source packages requiring compilation (Spack)
- **package_manager**: Traditional package managers (future: conda, pip)
- **environment_module**: Module-based software stacks

## Loading Software Catalogs

### EESSI Catalog Loading

The EESSI loader uses the new EESSI API format which supports both main software packages and extensions (Python packages, R packages, etc.).

#### Load EESSI Catalog

```bash
# Load EESSI catalog (dry run first to see what will be created)
DJANGO_SETTINGS_MODULE=waldur_core.server.settings uv run waldur load_eessi_catalog --dry-run

# Load the actual catalog with extensions
DJANGO_SETTINGS_MODULE=waldur_core.server.settings uv run waldur load_eessi_catalog

# Load without extensions
DJANGO_SETTINGS_MODULE=waldur_core.server.settings uv run waldur load_eessi_catalog --no-extensions

# Update existing catalog with new data
DJANGO_SETTINGS_MODULE=waldur_core.server.settings uv run waldur load_eessi_catalog --update-existing
```

#### EESSI Command Options

- `--catalog-name`: Name of the software catalog (default: EESSI)
- `--catalog-version`: EESSI version (auto-detected from API if not provided)
- `--api-url`: Base URL for EESSI API (default: <https://www.eessi.io/api_data/data/>)
- `--extensions/--no-extensions`: Include/exclude extension packages (default: include)
- `--dry-run`: Show what would be done without making changes
- `--update-existing`: Update existing catalog data if it exists

### Spack Catalog Loading

The Spack loader supports the repology.json format from packages.spack.io, providing access to thousands of scientific computing packages.

#### Load Spack Catalog

```bash
# Load Spack catalog (dry run first to see what will be created)
DJANGO_SETTINGS_MODULE=waldur_core.server.settings uv run waldur load_spack_catalog --dry-run

# Load the actual catalog
DJANGO_SETTINGS_MODULE=waldur_core.server.settings uv run waldur load_spack_catalog

# Load with custom data URL
DJANGO_SETTINGS_MODULE=waldur_core.server.settings uv run waldur load_spack_catalog \
  --data-url "https://custom.spack.site/data/repology.json"

# Update existing catalog
DJANGO_SETTINGS_MODULE=waldur_core.server.settings uv run waldur load_spack_catalog --update-existing
```

#### Spack Command Options

- `--catalog-name`: Name of the software catalog (default: Spack)
- `--catalog-version`: Spack version (auto-detected from data timestamp if not provided)
- `--data-url`: URL for Spack repology.json data
- `--dry-run`: Show what would be done without making changes
- `--update-existing`: Update existing catalog data if it exists

### What Gets Created

Both loaders create:

- **SoftwareCatalog** entry with detected version and metadata
- **SoftwarePackage** entries for each software package
- **SoftwareVersion** entries for each package version
- **SoftwareTarget** entries for architecture/platform combinations or build variants

## Automated Catalog Updates

Waldur provides automated daily updates for software catalogs through Celery tasks.

### Configuration Settings

Configure automated updates through constance settings:

#### EESSI Settings

- `SOFTWARE_CATALOG_EESSI_UPDATE_ENABLED`: Enable automated EESSI updates (default: true)
- `SOFTWARE_CATALOG_EESSI_VERSION`: EESSI version to load (auto-detect if empty)
- `SOFTWARE_CATALOG_EESSI_API_URL`: Base URL for EESSI API data
- `SOFTWARE_CATALOG_EESSI_INCLUDE_EXTENSIONS`: Include Python/R extensions (default: true)

#### Spack Settings

- `SOFTWARE_CATALOG_SPACK_UPDATE_ENABLED`: Enable automated Spack updates (default: true)
- `SOFTWARE_CATALOG_SPACK_VERSION`: Spack version to load (auto-detect if empty)
- `SOFTWARE_CATALOG_SPACK_DATA_URL`: URL for Spack repology.json data

#### General Settings

- `SOFTWARE_CATALOG_UPDATE_EXISTING_PACKAGES`: Update existing packages during refresh (default: true)
- `SOFTWARE_CATALOG_CLEANUP_ENABLED`: Enable automatic cleanup of old catalog data (default: false)
- `SOFTWARE_CATALOG_RETENTION_DAYS`: Number of days to retain old catalog versions (default: 90)

### Scheduled Updates

The `update_software_catalogs` task runs daily at 3 AM and:

1. **Independent Processing**: Each catalog is updated independently - failures don't affect other catalogs
2. **Configuration Validation**: Validates settings before attempting updates
3. **Error Isolation**: Individual catalog failures are logged but don't prevent other updates
4. **Comprehensive Logging**: Detailed logging for monitoring and troubleshooting

### Manual Trigger

You can manually trigger catalog updates:

```bash
# Trigger all enabled catalog updates
DJANGO_SETTINGS_MODULE=waldur_core.server.settings uv run waldur celery call marketplace.update_software_catalogs
```

## Associate Catalogs with Offerings

Link the loaded software catalogs to your marketplace offerings:

```bash
# Find your offering and catalog UUIDs
# List offerings and catalogs using REST API
curl "https://your-waldur.example.com/api/marketplace-provider-offerings/"
curl "https://your-waldur.example.com/api/marketplace-software-catalogs/"

# Associate catalog with offering via API
curl -X POST "https://your-waldur.example.com/api/marketplace-provider-offerings/<offering_uuid>/add_software_catalog/" \
  -H "Authorization: Token your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "catalog": "<catalog_uuid>",
    "enabled_cpu_family": ["x86_64", "aarch64"],
    "enabled_cpu_microarchitectures": ["generic"]
  }'
```

## Understanding Software Catalog Targets

### EESSI Architecture Targets

EESSI provides software optimized for different CPU architectures and microarchitectures:

#### Common CPU Targets

- `x86_64/generic` - General x86_64 compatibility
- `x86_64/intel/haswell` - Intel Haswell and newer
- `x86_64/intel/skylake_avx512` - Intel Skylake with AVX-512
- `x86_64/amd/zen2` - AMD Zen2 architecture
- `x86_64/amd/zen3` - AMD Zen3 architecture
- `aarch64/generic` - General ARM64 compatibility
- `aarch64/neoverse_n1` - ARM Neoverse N1 cores

#### EESSI Extension Support

The new EESSI API format includes support for extension packages:

- **Python packages**: NumPy, SciPy, TensorFlow, PyTorch, etc.
- **R packages**: Bioconductor, CRAN packages
- **Perl modules**: CPAN modules
- **Ruby gems**: Scientific Ruby libraries
- **Octave packages**: Signal processing, optimization

Extensions are linked to their parent software (e.g., Python packages linked to Python installation).

### Spack Build Variants

Spack supports flexible build configurations through targets:

#### Target Types

- `build_variant/default` - Standard build configuration
- `platform/windows` - Windows-compatible packages
- `external/system` - System-provided packages (detectable)
- `build_system/build-tool` - Build tools and compilers

#### Spack Categories

- `build-tools` - Compilers, build systems, make tools
- `detectable` - Externally provided packages
- `windows` - Windows compatibility
- Custom categories based on package metadata

### Why Targets Matter

1. **Performance**: Architecture-specific builds can be 20-50% faster
2. **Compatibility**: Ensures software runs on target hardware
3. **Instruction Sets**: Leverages specific CPU features (AVX, NEON, etc.)
4. **HPC Requirements**: Critical for scientific computing workloads
5. **Build Flexibility**: Spack provides multiple build configurations

## Available API Endpoints

The software catalog system provides the following API endpoints:

- **marketplace-software-catalogs**: View and manage software catalogs
- **marketplace-software-packages**: Browse software packages within catalogs
- **marketplace-software-versions**: View software versions for packages
- **marketplace-software-targets**: View architecture-specific installations

### Software Catalog Management Actions

Offering-software catalog associations are managed through offering actions:

- `add_software_catalog`: Associate a catalog with an offering
- `update_software_catalog`: Update catalog configuration for an offering
- `remove_software_catalog`: Remove catalog association from offering

These actions are available on the `marketplace-provider-offerings` endpoint.

## API Usage

### Browse Available Catalogs

```bash
# List all software catalogs
curl "https://your-waldur.example.com/api/marketplace-software-catalogs/"

# Filter catalogs by name
curl "https://your-waldur.example.com/api/marketplace-software-catalogs/?name=EESSI"
```

Example response:

```json
{
  "count": 1,
  "results": [
    {
      "url": "https://your-waldur.example.com/api/marketplace-software-catalogs/abc-123/",
      "uuid": "abc-123-def-456",
      "name": "EESSI",
      "version": "2023.06",
      "source_url": "https://software.eessi.io/",
      "description": "European Environment for Scientific Software Installations",
      "package_count": 582
    }
  ]
}
```

### Browse Software Packages

```bash
# List packages in a catalog
curl "https://your-waldur.example.com/api/marketplace-software-packages/?catalog_uuid=abc-123-def-456"

# Search for specific software by name
curl "https://your-waldur.example.com/api/marketplace-software-packages/?name=sampleapp"

# Search across name, description, and versions
curl "https://your-waldur.example.com/api/marketplace-software-packages/?query=computing"

# Filter by offering and catalog version
curl "https://your-waldur.example.com/api/marketplace-software-packages/?offering_uuid=def-456&catalog_version=2023.06"

# Order by catalog version
curl "https://your-waldur.example.com/api/marketplace-software-packages/?o=catalog_version"
```

Example response:

```json
{
  "count": 582,
  "results": [
    {
      "url": "https://your-waldur.example.com/api/marketplace-software-packages/package-uuid/",
      "uuid": "package-uuid",
      "name": "SampleApp",
      "description": "Scientific computing application...",
      "homepage": "https://example.com/sampleapp",
      "catalog": "abc-123-def-456",
      "version_count": 12
    }
  ]
}
```

### Package Detail with Nested Versions and Targets

When viewing package details, the response includes nested versions with their targets:

```bash
# Get package detail with nested versions and targets
curl "https://your-waldur.example.com/api/marketplace-software-packages/package-uuid/"
```

Example detailed response:

```json
{
  "uuid": "package-uuid",
  "name": "SampleApp",
  "description": "Scientific computing application...",
  "homepage": "https://example.com/sampleapp",
  "catalog": "abc-123-def-456",
  "version_count": 2,
  "versions": [
    {
      "uuid": "version-uuid-1",
      "version": "1.2.0",
      "release_date": "2023-06-15",
      "metadata": {},
      "targets": [
        {
          "uuid": "target-uuid-1",
          "cpu_family": "x86_64",
          "cpu_microarchitecture": "generic",
          "path": "/cvmfs/software.eessi.io/versions/2023.06/software/linux/x86_64/generic"
        },
        {
          "uuid": "target-uuid-2",
          "cpu_family": "aarch64",
          "cpu_microarchitecture": "generic",
          "path": "/cvmfs/software.eessi.io/versions/2023.06/software/linux/aarch64/generic"
        }
      ]
    },
    {
      "uuid": "version-uuid-2",
      "version": "1.3.0",
      "release_date": "2023-08-20",
      "metadata": {},
      "targets": [
        {
          "uuid": "target-uuid-3",
          "cpu_family": "x86_64",
          "cpu_microarchitecture": "generic",
          "path": "/cvmfs/software.eessi.io/versions/2023.06/software/linux/x86_64/generic"
        }
      ]
    }
  ]
}
```

### Browse Software Versions

```bash
# Get versions for a package
curl "https://your-waldur.example.com/api/marketplace-software-versions/?package_uuid=package-uuid"

# Filter by CPU family
curl "https://your-waldur.example.com/api/marketplace-software-versions/?package_uuid=package-uuid&cpu_family=x86_64"
```

### Browse Installation Targets

```bash
# Get available targets for a version
curl "https://your-waldur.example.com/api/marketplace-software-targets/?version_uuid=version-uuid"

# Filter by CPU family
curl "https://your-waldur.example.com/api/marketplace-software-targets/?cpu_family=x86_64"

# Filter by CPU microarchitecture
curl "https://your-waldur.example.com/api/marketplace-software-targets/?cpu_microarchitecture=generic"
```

## Linking Catalogs to Offerings

### Associate Catalog with Offering

Offering-software catalog associations are managed through offering actions, not a separate endpoint:

```bash
# Add software catalog to offering
curl -X POST "https://your-waldur.example.com/api/marketplace-provider-offerings/{offering_uuid}/add_software_catalog/" \
  -H "Authorization: Token your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "catalog": "catalog-uuid",
    "enabled_cpu_family": ["x86_64", "aarch64"],
    "enabled_cpu_microarchitectures": ["generic"]
  }'
```

### Update Offering Software Catalog Configuration

```bash
# Update software catalog configuration for an offering
curl -X PATCH "https://your-waldur.example.com/api/marketplace-provider-offerings/{offering_uuid}/update_software_catalog/" \
  -H "Authorization: Token your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "offering_catalog_uuid": "offering-catalog-uuid",
    "enabled_cpu_family": ["x86_64", "aarch64"],
    "enabled_cpu_microarchitectures": ["generic", "zen3"]
  }'
```

### Remove Software Catalog from Offering

```bash
# Remove software catalog from offering
curl -X POST "https://your-waldur.example.com/api/marketplace-provider-offerings/{offering_uuid}/remove_software_catalog/" \
  -H "Authorization: Token your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "offering_catalog_uuid": "offering-catalog-uuid"
  }'
```

### Query Offering Software

```bash
# Get offering details with associated software catalogs
curl "https://your-waldur.example.com/api/marketplace-provider-offerings/{offering_uuid}/"

# Get software packages available for an offering
curl "https://your-waldur.example.com/api/marketplace-software-packages/?offering_uuid=offering-uuid"

## Catalog Management Commands

### Available Commands

The software catalog system provides management commands for different catalog types:

- **load_eessi_catalog**: Load EESSI catalogs using the new API format
- **load_spack_catalog**: Load Spack catalogs from repology.json format

### Common Command Features

All catalog loading commands support:

- `--dry-run`: Preview changes without modifying the database
- `--update-existing`: Update existing packages and versions
- Automatic version detection from source data
- Comprehensive error handling and logging
- Statistics reporting on created/updated records

### Data Loading Process

The unified catalog loader framework follows this process:

1. **Validation**: Verify command arguments and connectivity
2. **Fetch**: Download catalog data from remote sources
3. **Transform**: Convert source format to unified data models
4. **Load**: Create or update database records
5. **Report**: Provide statistics and completion status

Both loaders handle:

- **Extension packages**: Link child packages to parent software
- **Multiple architectures**: Support diverse target platforms
- **Metadata preservation**: Store catalog-specific information
- **Error recovery**: Continue processing despite individual failures

## Permissions

### Catalog Management (Staff Only)

- **SoftwareCatalog**: Only staff can create/modify catalogs
- **SoftwarePackage**: Only staff can manage package information
- **SoftwareVersion**: Only staff can manage version data
- **SoftwareTarget**: Only staff can manage target information

### Offering Integration (Offering Managers)

- **OfferingSoftwareCatalog**: Offering managers can associate catalogs with their offerings through offering actions (`add_software_catalog`, `update_software_catalog`, `remove_software_catalog`)

## Integration Details

### EESSI New API Format

The EESSI loader uses the new API format that separates main software and extensions.

#### Main Software Structure

```json

{
  "timestamp": "2024-12-02T10:00:00Z",
  "architectures_map": {
    "2023.06": ["x86_64/generic", "aarch64/generic", "x86_64/zen3"]
  },
  "software": {
    "Python": {
      "description": "Python programming language",
      "homepage": "https://www.python.org/",
      "categories": ["lang"],
      "versions": [
        {
          "version": "3.11.3",
          "cpu_arch": ["x86_64/generic", "aarch64/generic"],
          "toolchain": {"name": "GCCcore", "version": "12.3.0"},
          "required_modules": ["GCCcore/12.3.0"],
          "modulename": "Python/3.11.3-GCCcore-12.3.0"
        }
      ]
    }
  }
}

```

#### Extension Structure

```json

{
  "timestamp": "2024-12-02T10:00:00Z",
  "software": {
    "numpy": {
      "description": "Fundamental package for array computing with Python",
      "homepage": "https://numpy.org/",
      "categories": ["math", "lib"],
      "versions": [
        {
          "version": "1.24.2",
          "cpu_arch": ["x86_64/generic"],
          "parent_software": {"name": "Python", "version": "3.11.3"},
          "modulename": "numpy/1.24.2-gfbf-2023a"
        }
      ]
    }
  }
}

```

### Spack Repology Format

Spack uses the repology.json format from packages.spack.io:

```json

{
  "last_update": "2024-12-02 10:00:00",
  "num_packages": 8000,
  "packages": {
    "cmake": {
      "summary": "A cross-platform, open-source build system",
      "homepages": ["https://cmake.org"],
      "categories": ["build-tools"],
      "licenses": ["BSD-3-Clause"],
      "maintainers": ["kitware-spack"],
      "version": [
        {
          "version": "3.28.1",
          "downloads": ["https://github.com/Kitware/CMake/releases/download/v3.28.1/cmake-3.28.1.tar.gz"]
        }
      ],
      "dependencies": ["openssl", "ncurses"]
    }
  }
}

```

### Catalog Metadata Comparison

| Feature | EESSI | Spack |
|---------|-------|-------|
| **Format** | New API (JSON) | Repology (JSON) |
| **Type** | Binary runtime | Source packages |
| **Architecture Support** | CPU-specific builds | Build variants |
| **Extensions** | Python, R, Perl, etc. | Dependencies only |
| **Toolchain Info** | Full toolchain details | Build dependencies |
| **Installation Paths** | CVMFS paths | Download URLs |
| **Categories** | Scientific domains | Package types |
| **Updates** | API timestamp | Git commit date |

## SLURM Partitions and Software Catalogs

For detailed information about SLURM partition configuration and their integration with software catalogs, see the dedicated [Marketplace SLURM Partitions](marketplace-slurm-partitions.md) guide.

This includes:
- SLURM partition model configuration
- Partition management APIs (add, update, remove)
- Partition-specific software catalog associations
- CPU architecture targeting for different partitions
