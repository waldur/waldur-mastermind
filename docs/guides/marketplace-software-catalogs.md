# Marketplace Software Catalogs

This guide covers the software catalog system in Waldur's marketplace, using the European Environment for Scientific Software Installations (EESSI) as a primary example.

## Overview

The software catalog system allows marketplace offerings to expose large collections of scientific and HPC software packages from external catalogs like EESSI. Instead of manually tracking individual software installations, offerings can reference comprehensive software catalogs with thousands of packages.

## Architecture

The system uses relational models for efficient storage and querying:

- **SoftwareCatalog**: Represents a software catalog (e.g., EESSI 2023.06)
- **SoftwarePackage**: Individual software packages within catalogs
- **SoftwareVersion**: Specific versions of packages
- **SoftwareTarget**: Architecture/platform-specific installations
- **OfferingSoftwareCatalog**: Links offerings to available catalogs

## Loading EESSI Catalog Data

### 1. Download EESSI Data

First, download the latest EESSI software catalog data:

```bash
# Download the latest EESSI catalog data
curl -o eessi.model.json https://www.eessi.io/docs/available_software/data/json_data_detail.json
```

### 2. Load Software Catalog Data

Use the EESSI management command to load catalog data:

```bash
# Load EESSI catalog (dry run first to see what will be created)
DJANGO_SETTINGS_MODULE=waldur_core.server.settings uv run python manage.py load_eessi_catalog --dry-run

# Load the actual catalog
DJANGO_SETTINGS_MODULE=waldur_core.server.settings uv run python manage.py load_eessi_catalog

# Update existing catalog with new data
DJANGO_SETTINGS_MODULE=waldur_core.server.settings uv run python manage.py load_eessi_catalog --update-existing
```

This creates:

- **SoftwareCatalog** entry for EESSI with detected version
- **SoftwarePackage** entries for each software package
- **SoftwareVersion** entries for each package version
- **SoftwareTarget** entries for each architecture/platform combination

### 3. Associate Catalogs with Offerings

Link the loaded software catalogs to your marketplace offerings:

```bash
# Find your offering and catalog UUIDs
DJANGO_SETTINGS_MODULE=waldur_core.server.settings uv run python manage.py shell -c "
from waldur_mastermind.marketplace.models import Offering, SoftwareCatalog

print('Available Offerings:')
for offering in Offering.objects.all()[:10]:
    print(f'{offering.uuid} - {offering.name}')

print('\nAvailable Software Catalogs:')
for catalog in SoftwareCatalog.objects.all():
    print(f'{catalog.uuid} - {catalog.name} {catalog.version}')
"

# Create offering-catalog association via API
curl -X POST "https://your-waldur.example.com/api/marketplace-offering-software-catalogs/" \
  -H "Authorization: Token your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "offering": "<offering_uuid>",
    "catalog": "<catalog_uuid>",
    "enabled_cpu_family": ["x86_64", "aarch64"],
    "enabled_cpu_microarchitectures": ["generic"]
  }'
```

## Understanding EESSI Architecture Targets

EESSI provides software optimized for different CPU architectures and microarchitectures:

### Common Targets

- `x86_64/generic` - General x86_64 compatibility
- `x86_64/intel/haswell` - Intel Haswell and newer
- `x86_64/intel/skylake_avx512` - Intel Skylake with AVX-512
- `x86_64/amd/zen2` - AMD Zen2 architecture
- `x86_64/amd/zen3` - AMD Zen3 architecture
- `aarch64/generic` - General ARM64 compatibility
- `aarch64/neoverse_n1` - ARM Neoverse N1 cores

### Why Targets Matter

1. **Performance**: Architecture-specific builds can be 20-50% faster
2. **Compatibility**: Ensures software runs on target hardware
3. **Instruction Sets**: Leverages specific CPU features (AVX, NEON, etc.)
4. **HPC Requirements**: Critical for scientific computing workloads

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

# Search for specific software
curl "https://your-waldur.example.com/api/marketplace-software-packages/?search=python"

# Filter by offering and architecture
curl "https://your-waldur.example.com/api/marketplace-software-packages/?offering_uuid=def-456&architecture=x86_64"
```

Example response:

```json
{
  "count": 582,
  "results": [
    {
      "url": "https://your-waldur.example.com/api/marketplace-software-packages/package-uuid/",
      "uuid": "package-uuid",
      "name": "Python",
      "description": "Python is a programming language...",
      "homepage": "https://www.python.org/",
      "catalog": "abc-123-def-456",
      "version_count": 12
    }
  ]
}
```

### Browse Software Versions

```bash
# Get versions for a package
curl "https://your-waldur.example.com/api/marketplace-software-versions/?package_uuid=package-uuid"

# Filter by architecture
curl "https://your-waldur.example.com/api/marketplace-software-versions/?package_uuid=package-uuid&architecture=x86_64"
```

### Browse Installation Targets

```bash
# Get available targets for a version
curl "https://your-waldur.example.com/api/marketplace-software-targets/?version_uuid=version-uuid"

# Filter by architecture pattern
curl "https://your-waldur.example.com/api/marketplace-software-targets/?architecture=x86_64"
```

## Linking Catalogs to Offerings

### Associate Catalog with Offering

```bash
# Create offering-catalog association
curl -X POST "https://your-waldur.example.com/api/marketplace-offering-software-catalogs/" \
  -H "Authorization: Token your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "offering": "offering-uuid",
    "catalog": "catalog-uuid",
    "enabled_cpu_family": ["x86_64", "aarch64"],
    "enabled_cpu_microarchitectures": ["generic"]
  }'
```

### Query Offering Software

```bash
# Get catalogs available for an offering
curl "https://your-waldur.example.com/api/marketplace-offering-software-catalogs/?offering_uuid=offering-uuid"

# Get software packages available for an offering
curl "https://your-waldur.example.com/api/marketplace-software-packages/?offering_uuid=offering-uuid"
```

## Command Options

The `load_eessi_catalog` command supports several options:

- `--json-file`: Path to EESSI JSON file (default: eessi.model.json)
- `--catalog-name`: Name of the software catalog (default: EESSI)
- `--catalog-version`: EESSI catalog version (auto-detected from JSON if not provided)
- `--dry-run`: Show what would be done without making changes
- `--update-existing`: Update existing catalog data if it exists
- `--no-sync`: Disable synchronization (default: sync enabled to remove missing records)

### Synchronization Behavior

By default, the command synchronizes the database with the JSON file:

- **Adds** new packages, versions, and targets from the JSON
- **Removes** packages, versions, and targets not present in the JSON
- **Preserves** existing records that match the JSON data

Use `--no-sync` to disable removal of missing records and only add new data.

## Permissions

### Catalog Management (Staff Only)

- **SoftwareCatalog**: Only staff can create/modify catalogs
- **SoftwarePackage**: Only staff can manage package information
- **SoftwareVersion**: Only staff can manage version data
- **SoftwareTarget**: Only staff can manage target information

### Offering Integration (Offering Managers)

- **OfferingSoftwareCatalog**: Offering managers can associate catalogs with their offerings

## EESSI Integration Details

### EESSI Data Structure

EESSI provides software in a structured format:

```json
{
  "targets": [
    "/cvmfs/software.eessi.io/versions/2023.06/software/linux/x86_64/generic",
    "/cvmfs/software.eessi.io/versions/2023.06/software/linux/aarch64/generic"
  ],
  "software": {
    "Python": {
      "description": "Python is a programming language...",
      "homepage": "https://www.python.org/",
      "versions": {
        "3.9.6": {
          "versionsuffix": "",
          "toolchain": "GCCcore/11.2.0"
        }
      }
    }
  }
}
```

## Offering Partitions

Offering partitions represent SLURM partitions associated with marketplace offerings. They define resource limits, scheduling policies, and access controls for different compute partitions.

### Partition Configuration

```bash
# Create a partition for an offering
curl -X POST "https://your-waldur.example.com/api/marketplace-offering-partitions/" \
  -H "Authorization: Token your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "offering": "offering-uuid",
    "partition_name": "gpu-partition",
    "max_cpus_per_node": 64,
    "def_mem_per_cpu": 4096,
    "max_time": 1440,
    "priority_tier": 50,
    "qos": "normal"
  }'
```

### Partition Parameters

#### CPU Configuration

- `cpu_bind`: Default task binding policy (SLURM cpu_bind)
- `def_cpu_per_gpu`: Default CPUs allocated per GPU
- `max_cpus_per_node`: Maximum allocated CPUs per node
- `max_cpus_per_socket`: Maximum allocated CPUs per socket

#### Memory Configuration (in MB)

- `def_mem_per_cpu`: Default memory per CPU
- `def_mem_per_gpu`: Default memory per GPU
- `def_mem_per_node`: Default memory per node
- `max_mem_per_cpu`: Maximum memory per CPU
- `max_mem_per_node`: Maximum memory per node

#### Time Limits

- `default_time`: Default time limit in minutes
- `max_time`: Maximum time limit in minutes
- `grace_time`: Preemption grace time in seconds

#### Node Configuration

- `max_nodes`: Maximum nodes per job
- `min_nodes`: Minimum nodes per job
- `exclusive_topo`: Exclusive topology access required
- `exclusive_user`: Exclusive user access required

#### Scheduling Configuration

- `priority_tier`: Priority tier for scheduling and preemption
- `qos`: Quality of Service (QOS) name
- `req_resv`: Require reservation for job allocation

### Linking Software Catalogs to Partitions

Software catalogs can be associated with specific partitions to control software availability:

```bash
# Associate catalog with a specific partition
curl -X POST "https://your-waldur.example.com/api/marketplace-offering-software-catalogs/" \
  -H "Authorization: Token your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "offering": "offering-uuid",
    "catalog": "catalog-uuid",
    "enabled_cpu_family": ["x86_64"],
    "enabled_cpu_microarchitectures": ["generic", "zen3"],
    "partition": "partition-uuid"
  }'
```

### Query Partitions

```bash
# List all partitions for an offering
curl "https://your-waldur.example.com/api/marketplace-offering-partitions/?offering_uuid=offering-uuid"

# Get partition details
curl "https://your-waldur.example.com/api/marketplace-offering-partitions/partition-uuid/"

# List software catalogs for a specific partition
curl "https://your-waldur.example.com/api/marketplace-offering-software-catalogs/?partition_uuid=partition-uuid"
```

## Updated Field Names

**Important**: The field names have been updated to use more precise terminology:

### Old vs New Field Names

| Old Field Name | New Field Name | Description |
|---|---|---|
| `enabled_architectures` | `enabled_cpu_family` | CPU architecture families (x86_64, aarch64) |
| `enabled_platforms` | `enabled_cpu_microarchitectures` | CPU microarchitectures (generic, zen3, neoverse_n1) |
| `architecture` | `cpu_family` | In SoftwareTarget model |
| `platform` | `cpu_microarchitecture` | In SoftwareTarget model |

### API Examples with Updated Fields

```bash
# Create offering-catalog association with new field names
curl -X POST "https://your-waldur.example.com/api/marketplace-offering-software-catalogs/" \
  -H "Authorization: Token your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "offering": "offering-uuid",
    "catalog": "catalog-uuid",
    "enabled_cpu_family": ["x86_64", "aarch64"],
    "enabled_cpu_microarchitectures": ["generic", "zen3", "neoverse_n1"]
  }'

# Filter software targets by CPU family
curl "https://your-waldur.example.com/api/marketplace-software-targets/?cpu_family=x86_64"

# Filter by CPU microarchitecture
curl "https://your-waldur.example.com/api/marketplace-software-targets/?cpu_microarchitecture=zen3"
```
