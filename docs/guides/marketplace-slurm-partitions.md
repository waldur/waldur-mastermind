# Marketplace SLURM Partitions and Software Catalogs

This guide covers SLURM partition configuration and their integration with software catalogs in Waldur's marketplace.

## Overview

SLURM partitions represent compute partitions in a cluster that can be associated with marketplace offerings. They define resource limits, scheduling policies, access controls, and optionally link to software catalogs for partition-specific software availability.

## SLURM Partition Model

The OfferingPartition model maps closely to SLURM's partition_info_t struct and includes comprehensive configuration options for HPC environments.

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
