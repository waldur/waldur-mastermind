# Marketplace SLURM Partitions and Software Catalogs

This guide covers SLURM partition configuration and their integration with software catalogs in Waldur's marketplace.

## Overview

SLURM partitions represent compute partitions in a cluster that can be associated with marketplace offerings. They define resource limits, scheduling policies, access controls, and optionally link to software catalogs for partition-specific software availability.

`OfferingPartition` records are exposed via the marketplace API for tools like Open OnDemand and are **informational by default**. The Waldur Site Agent can optionally enforce them as access restrictions on the SLURM cluster (`sacctmgr add user … Partitions=…`), enabling per-partition pricing — one offering per partition, each with its own price components — while reusing the same underlying SLURM account hierarchy. Enforcement is opt-in; existing deployments that populated partitions for documentation purposes only continue to behave exactly as before.

## SLURM partition assignment by the Site Agent

Enforcement is enabled per-cluster via the `enforce_offering_partitions` setting in the agent's `backend_settings`. The default is `false` — partition records are not threaded into SLURM. When set to `true`, and when an offering has `OfferingPartition` records, the agent constructs an association command that includes the offering's partition list:

```bash
sacctmgr add user <username> account=<account> DefaultAccount=<default> \
    Partitions=p1,p2 Share=parent
```

Behavior summary (when enforcement is enabled):

- The offering's partition list is read at user-association time. Partition names are sorted alphabetically and joined with commas into a single `Partitions=` argument.
- The agent **does not** reconcile partition associations after creation. Changes to an offering's partition list affect only newly-added users; users who already have associations keep their existing partition restrictions until they are explicitly removed and re-added.
- The agent does **not** emit a per-user `DefaultPartition=`. Real `sacctmgr` does not accept that attribute on `add user` (no parser in `user_functions.c` or `sacctmgr_set_assoc_rec`) and rejects the call with `Unknown option`. The default partition for unparameterized jobs comes from the cluster-wide `Default=YES` line in `slurm.conf`.

### Precedence

The agent resolves partitions in this order:

1. **Offering partitions** — when `enforce_offering_partitions` is `true` and the offering has `OfferingPartition` records, those names become the `Partitions=` value.
2. **Global `default_partition`** — when the offering has no partitions (or enforcement is disabled), the agent's `default_partition` setting (single partition string) is used as a fallback. This preserves the pre-existing single-partition behavior for sites that haven't migrated to per-offering partitions.
3. **Unrestricted** — neither configured, no `Partitions=` flag is emitted. The user falls back to SLURM's cluster-wide default partition behavior.

### Site-agent configuration

Two relevant settings under `backend_settings`:

```yaml
backend_settings:
  enforce_offering_partitions: true    # Opt-in; default false (informational only)
  default_partition: "cn"              # Fallback when offering has no partitions
```

- `enforce_offering_partitions` switches on the partition-aware path. Leave unset (or set to `false`) to keep `OfferingPartition` records purely informational, the historical behavior.
- `default_partition` is the legacy single-partition fallback used when the offering has no partitions or when enforcement is disabled.

### Scope and non-goals

- Partition restrictions are applied at **user-level** SLURM associations. SLURM's accounting model does not support partition restrictions at account scope.
- The agent does **not** modify existing user associations when an offering's partition list changes. To rebalance, an operator must remove and re-add the user, or terminate the resource and re-provision it on the desired offering.
- Other partition attributes (`max_cpus_per_node`, `max_time`, QoS, etc.) remain informational — they are exposed via the API but are not pushed into SLURM by the agent.

## SLURM Partition Model

The OfferingPartition model maps closely to SLURM's partition_info_t struct and includes comprehensive configuration options for HPC environments.

### Partition Parameters

#### Architecture

- `cpu_arch`: CPU architecture of the partition (e.g., `x86_64/amd/zen3`)
- `gpu_arch`: GPU architecture of the partition (e.g., `nvidia/cc90`, `amd/gfx90a`)

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

## Partition Management API

### Available Endpoints

Partition management is handled through offering actions, similar to software catalog management:

- `add_partition`: Add a new partition to an offering
- `update_partition`: Update partition configuration
- `remove_partition`: Remove a partition from an offering

### Add Partition to Offering

```bash
# Add partition to offering
curl -X POST "https://your-waldur.example.com/api/marketplace-provider-offerings/{offering_uuid}/add_partition/" \
  -H "Authorization: Token your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "partition_name": "gpu-partition",
    "cpu_arch": "x86_64/amd/zen3",
    "gpu_arch": "nvidia/cc90",
    "max_cpus_per_node": 64,
    "max_mem_per_node": 512000,
    "max_time": 2880,
    "default_time": 60,
    "qos": "gpu",
    "priority_tier": 1
  }'
```

### Update Partition Configuration

```bash
# Update partition configuration
curl -X PATCH "https://your-waldur.example.com/api/marketplace-provider-offerings/{offering_uuid}/update_partition/" \
  -H "Authorization: Token your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "partition_uuid": "partition-uuid",
    "max_time": 4320,
    "priority_tier": 2
  }'
```

### Remove Partition from Offering

```bash
# Remove partition from offering
curl -X POST "https://your-waldur.example.com/api/marketplace-provider-offerings/{offering_uuid}/remove_partition/" \
  -H "Authorization: Token your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "partition_uuid": "partition-uuid"
  }'
```

## Partition Software Catalog Associations

Software catalogs can be optionally associated with specific partitions through the `partition` field in OfferingSoftwareCatalog. This enables partition-specific software availability, allowing different partitions to expose different software sets.

### Associating Software Catalogs with Partitions

```bash
# Add software catalog to specific partition
curl -X POST "https://your-waldur.example.com/api/marketplace-provider-offerings/{offering_uuid}/add_software_catalog/" \
  -H "Authorization: Token your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "catalog": "catalog-uuid",
    "enabled_cpu_family": ["x86_64"],
    "enabled_cpu_microarchitectures": ["generic"],
    "partition": "partition-uuid"
  }'
```

### Use Cases for Partition-Specific Software

1. **Architecture-Specific Partitions**: GPU partitions with CUDA libraries, ARM partitions with ARM-optimized software
2. **License Management**: Commercial software available only on specific partitions
3. **Performance Optimization**: Different optimized builds for different hardware configurations
4. **Access Control**: Research groups with access to specialized software on designated partitions

## Example Workflow

Here's a complete example of setting up a GPU partition with specialized software:

```bash
# 1. Add GPU partition
curl -X POST "https://your-waldur.example.com/api/marketplace-provider-offerings/{offering_uuid}/add_partition/" \
  -H "Authorization: Token your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "partition_name": "gpu-v100",
    "cpu_arch": "x86_64/intel/skylake_avx512",
    "gpu_arch": "nvidia/cc70",
    "max_cpus_per_node": 40,
    "def_cpu_per_gpu": 4,
    "max_mem_per_node": 384000,
    "max_time": 2880,
    "default_time": 120,
    "qos": "gpu",
    "priority_tier": 1,
    "exclusive_user": true
  }'

# 2. Associate CUDA software catalog with GPU partition
curl -X POST "https://your-waldur.example.com/api/marketplace-provider-offerings/{offering_uuid}/add_software_catalog/" \
  -H "Authorization: Token your-token" \
  -H "Content-Type: application/json" \
  -d '{
    "catalog": "cuda-catalog-uuid",
    "enabled_cpu_family": ["x86_64"],
    "enabled_cpu_microarchitectures": ["skylake_avx512"],
    "partition": "gpu-partition-uuid"
  }'
```

## Partition Architecture Filtering

Partitions can be filtered by their CPU and GPU architecture fields, enabling users to find partitions matching specific hardware requirements.

### Available Filters

| Filter | Type | Description |
|--------|------|-------------|
| `cpu_arch` | string (icontains) | Filter by CPU architecture substring (e.g., `zen3`, `x86_64`) |
| `gpu_arch` | string (icontains) | Filter by GPU architecture substring (e.g., `nvidia`, `cc90`) |
| `has_gpu` | boolean | Filter partitions with (`true`) or without (`false`) GPU architecture |

### Examples

```bash
# Find partitions with AMD Zen3 CPUs
curl "https://your-waldur.example.com/api/marketplace-offering-partitions/?cpu_arch=zen3"

# Find partitions with NVIDIA GPUs
curl "https://your-waldur.example.com/api/marketplace-offering-partitions/?gpu_arch=nvidia"

# Find all GPU-equipped partitions
curl "https://your-waldur.example.com/api/marketplace-offering-partitions/?has_gpu=true"

# Find CPU-only partitions
curl "https://your-waldur.example.com/api/marketplace-offering-partitions/?has_gpu=false"
```

### Connecting Software to Partitions

The `gpu_arch` field on partitions and the `gpu_architectures` field on software targets enable matching software to compatible hardware. For example, to find which partitions can run software requiring `nvidia/cc90`:

```bash
# 1. Find software targets requiring nvidia/cc90
curl "https://your-waldur.example.com/api/marketplace-software-targets/?gpu_arch=nvidia/cc90"

# 2. Find partitions providing nvidia/cc90
curl "https://your-waldur.example.com/api/marketplace-offering-partitions/?gpu_arch=nvidia/cc90"
```

## Integration Considerations

### SLURM Configuration Mapping

When configuring OfferingPartition models, ensure the parameters align with your actual SLURM cluster configuration:

1. **Resource Limits**: Set realistic limits that match hardware capabilities
2. **QOS Integration**: Ensure QOS names match those defined in SLURM
3. **Time Limits**: Align with cluster policies and user expectations
4. **Architecture Targeting**: Match CPU families/microarchitectures with actual hardware

### Software Catalog Strategy

Consider these approaches when associating software catalogs with partitions:

1. **Global Catalog**: Single catalog available across all partitions
2. **Partition-Specific**: Different catalogs for different partition types
3. **Hybrid Approach**: Base catalog globally + specialized catalogs per partition

## Permissions

### Partition Management (Offering Managers)

- **OfferingPartition**: Offering managers can create/modify SLURM partition configurations through offering actions
- Requires `UPDATE_OFFERING` permission on the offering

### Software Catalog Association (Offering Managers)

- **OfferingSoftwareCatalog**: Offering managers can associate catalogs with partitions through offering actions
- Must have `UPDATE_OFFERING` permission on the offering

## Related Documentation

- [Marketplace Software Catalogs](marketplace-software-catalogs.md) - Main software catalog documentation
