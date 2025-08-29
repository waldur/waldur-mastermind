# OpenStack Replication Plugin

## Introduction

The OpenStack Replication plugin extends Waldur's capabilities by enabling cross-tenant migration and replication of OpenStack infrastructure. This plugin allows organizations to migrate complete OpenStack tenants between different OpenStack service providers or regions while preserving network configurations, security groups, and other infrastructure components.

### Core Functionality

The plugin enables:

- **Tenant Migration**: Complete migration of OpenStack tenants between service providers
- **Network Replication**: Copy network topologies, subnets, and routing configurations
- **Security Group Migration**: Replicate security groups and their rules with proper references
- **Port Synchronization**: Migrate port configurations including fixed IPs and security associations
- **Volume Type Mapping**: Map volume types between source and destination environments
- **Quota Preservation**: Maintain resource limits during migration
- **Selective Migration**: Choose specific networks and resources to migrate

## Architecture

### Core Models

#### Migration

The central model that tracks migration operations between OpenStack tenants:

```python
class Migration(TimeStampedModel, StateMixin, UuidMixin):
    created_by = ForeignKey(User)           # User who initiated migration
    src_resource = ForeignKey(Resource)     # Source marketplace resource
    dst_resource = ForeignKey(Resource)     # Destination marketplace resource
    mappings = JSONField()                  # Configuration mappings
```

**Key Features:**

- **State Tracking**: Uses `StateMixin` for migration progress monitoring
- **User Attribution**: Links migrations to specific users for auditing
- **Resource Linking**: Connects source and destination marketplace resources
- **Flexible Mappings**: JSON field stores complex migration configurations

**Permissions**: Only users who created migrations can access them

### Migration Configuration

#### Mapping Options

The migration system supports flexible mapping configurations:

```python
class MappingSerializer(serializers.Serializer):
    volume_types = VolumeTypeMappingSerializer(many=True)     # Volume type mappings
    subnets = SubNetMappingSerializer(many=True)             # Subnet CIDR mappings
    skip_connection_extnet = BooleanField(default=False)     # Skip external network connection
    sync_instance_ports = BooleanField(default=False)       # Enable port synchronization
    networks = SlugRelatedField(many=True)                  # Select specific networks
```

**Volume Type Mapping**:

- Maps source volume types to destination equivalents
- Preserves quota allocations across different storage backends
- Validates that types exist in respective environments

**Subnet Mapping**:

- Allows CIDR remapping for network conflicts
- Validates private subnet CIDR formats
- Automatically adjusts allocation pools for new CIDRs

## Migration Process

### 1. Migration Creation

**API Endpoint**: `POST /api/openstack-migrations/`

**Required Parameters**:

- `src_resource`: UUID of source marketplace resource (OpenStack tenant)
- `dst_offering`: UUID of destination marketplace offering
- `dst_plan`: UUID of plan for destination resource

**Optional Parameters**:

- `name`: Custom name for destination resource
- `description`: Description for destination resource
- `mappings`: Configuration object for advanced mapping options

### 2. Resource Replication

The creation process involves several atomic steps:

1. **Destination Tenant Creation**: Creates new tenant with generated credentials
2. **Network Topology Copy**: Replicates networks, subnets, and routing
3. **Security Group Migration**: Copies groups and rules with proper references
4. **Quota Application**: Maps and applies resource limits
5. **Marketplace Integration**: Creates destination resource record

### 3. Execution Pipeline

**MigrationExecutor** orchestrates the migration using Celery task chains:

```python
def get_task_signature(migration):
    tasks = [
        StateTransitionTask("begin_creating"),
        get_tenant_create_tasks(dst_tenant, skip_external_network),
        get_create_ports_tasks(src_tenant, dst_tenant, networks)  # Optional
    ]
    return chain(*tasks)
```

**Task Flow**:

1. **State Transition**: Mark migration as "creating"
2. **Tenant Provisioning**: Execute OpenStack tenant creation workflow
3. **Port Replication**: Copy ports if `sync_instance_ports` enabled

## Network Migration Details

### Network and Subnet Replication

The system performs network topology migration:

**Networks**:

- Preserves MTU settings and descriptions
- Maintains network names for consistency
- Creates equivalent networks in destination tenant

**Subnets**:

- Supports CIDR remapping via subnet mappings
- Preserves DNS nameservers and host routes
- Adjusts allocation pools for remapped CIDRs
- Maintains gateway configuration

**Example Network Selection**:

```json
{
  "mappings": {
    "networks": ["network-uuid-1", "network-uuid-2"],
    "subnets": [
      {"src_cidr": "192.168.1.0/24", "dst_cidr": "10.0.1.0/24"}
    ]
  }
}
```

### Security Group Migration

**Security Groups**:

- Copies all security groups with names and descriptions
- Maintains group relationships for inter-group references
- Creates equivalent groups in destination tenant

**Security Rules**:

- Replicates all rule configurations (protocol, ports, direction)
- Maps CIDR ranges according to subnet mappings
- Resolves remote group references after all groups are created
- Handles both ingress and egress rules

### Router Configuration

**Static Route Filtering**:

- Only migrates routes targeting destination subnet CIDRs
- Prevents invalid route configurations in new environment
- Preserves nexthop configurations where applicable

## Port Synchronization

### Advanced Port Migration

When `sync_instance_ports` is enabled, the system performs detailed port replication:

**Port Types Handled**:

- **Instance Ports**: Ports connected to active instances (state=OK)
- **VIP Ports**: Free ports in DOWN state for virtual IPs (`device_owner="compute:nova"`)

**Port Migration Process**:

1. **Data Collection**: Gather port configuration from source
2. **Security Group Mapping**: Map security groups to destination equivalents
3. **Port Creation**: Create port with preserved configuration
4. **Subnet Resolution**: Update fixed IPs to use destination subnet IDs
5. **Backend Provisioning**: Execute OpenStack port creation via backend

**Port Data Structure**:

```python
port_data = {
    "name": src_port.name,
    "description": src_port.description,
    "dst_tenant_id": dst_tenant.id,
    "dst_network_id": dst_network.id,
    "dst_subnet_id": dst_subnet.id,
    "port_security_enabled": src_port.port_security_enabled,
    "fixed_ips": src_port.fixed_ips,
    "mac_address": src_port.mac_address,
    "security_group_names": security_group_names
}
```

## Quota and Limit Handling

### Quota Preservation (`serializers.py:318`)

**Standard Limits**:

All marketplace limits are preserved:

- CPU cores, RAM, storage quotas
- Network and security group limits
- Instance and volume quotas

**Volume Type Quota Mapping**:

- Aggregates quotas for mapped volume types
- Handles multiple source types mapping to single destination type
- Preserves total storage allocation across type changes

**Quota Application (`serializers.py:376`)**:

```python
# Standard quotas from offering limits
quotas = map_limits_to_quotas(limits, dst_offering)

# Infrastructure quotas from source tenant
for quota_name in ("instances", "volumes", "snapshots",
                   "security_group_count", "security_group_rule_count"):
    quotas[quota_name] = src_tenant.get_quota_limit(quota_name)
```

## Event Handling

### Migration Lifecycle

**Order Creation**: When migration completes, marketplace orders are automatically created:

```python
def handle_migration_post_save(sender, instance: Migration, created, **kwargs):
    if migration.state in (CoreStates.OK, CoreStates.ERRED):
        Order.objects.create(
            resource=migration.dst_resource,
            offering=migration.dst_resource.offering,
            state=OrderStates.DONE if state == CoreStates.OK else OrderStates.ERRED,
            # ... additional order fields
        )
```

**Benefits**:

- Proper marketplace integration for billing and tracking
- Audit trail for migration operations
- Integration with approval workflows

## API Reference

### Migration Management

#### Create Migration

```http
POST /api/openstack-migrations/
Content-Type: application/json

{
  "src_resource": "source-tenant-uuid",
  "dst_offering": "destination-offering-uuid",
  "dst_plan": "destination-plan-uuid",
  "name": "Migrated Environment",
  "mappings": {
    "volume_types": [
      {"src_type_uuid": "uuid1", "dst_type_uuid": "uuid2"}
    ],
    "subnets": [
      {"src_cidr": "192.168.1.0/24", "dst_cidr": "10.0.1.0/24"}
    ],
    "networks": ["network-uuid-to-include"],
    "sync_instance_ports": true,
    "skip_connection_extnet": false
  }
}
```

#### List Migrations

```http
GET /api/openstack-migrations/
```

**Filters Available**:

- `src_resource_uuid`: Filter by source resource UUID
- `dst_resource_uuid`: Filter by destination resource UUID

#### Migration Details Response

```json
{
  "uuid": "migration-uuid",
  "created": "2023-01-01T00:00:00Z",
  "state": "OK",
  "src_offering_name": "Source OpenStack",
  "dst_offering_name": "Destination OpenStack",
  "src_resource_name": "Production Tenant",
  "dst_resource_name": "Migrated Production Tenant",
  "dst_resource_state": "OK",
  "mappings": {
    "volume_types": [...],
    "subnets": [...],
    "networks": [...],
    "sync_instance_ports": true
  }
}
```

## Configuration Options

### Network Selection

- **All Networks**: Default behavior migrates all networks and subnets
- **Selective Networks**: Use `networks` array to specify which networks to migrate
- **Subnet Remapping**: Provide CIDR mappings to avoid IP conflicts

### Port Synchronization Options

- **Instance Ports**: Automatically included when `sync_instance_ports=true`
- **VIP Ports**: Free ports for virtual IP configurations
- **Security Group Mapping**: Preserves security associations

### Volume Type Handling

- **One-to-One Mapping**: Map each source type to destination equivalent
- **Many-to-One Mapping**: Aggregate multiple source types to single destination
- **Quota Aggregation**: Automatically sums quotas for merged types

## Validation Rules

### Pre-Migration Checks (`serializers.py:156`)

1. **Source Resource Validation**:
  - Must have limits configured
  - Must be accessible to requesting user

2. **Destination Offering Validation**:
  - Must be available for ordering by user
  - Plan must belong to selected offering

3. **Permission Validation**:
  - User must have tenant creation permissions in target project
  - Order must not require consumer review (auto-approved)

4. **Mapping Validation**:
  - Volume types must exist in respective environments
  - Cannot combine `sync_instance_ports` with subnet mappings
  - Subnet CIDRs must be valid private network ranges

## Error Handling

### Migration Failures

- **Object Not Found**: Gracefully handles missing dependencies
- **Backend Errors**: Properly propagates OpenStack API failures
- **Validation Errors**: Clear error messages for configuration issues
- **State Management**: Failed migrations marked as ERRED with error details

### Recovery Mechanisms

- **Partial Success**: Network migration continues even if some components fail
- **Security Group Recovery**: Handles missing security groups during port creation
- **Route Validation**: Filters invalid routes to prevent configuration errors

## Integration with Marketplace

### Resource Lifecycle

1. **Resource Creation**: Destination resource created before migration starts
2. **Order Generation**: Marketplace order created on migration completion
3. **Billing Integration**: Proper cost tracking for migrated resources
4. **State Synchronization**: Resource state reflects migration progress

### Permissions Integration

- Uses marketplace permission system for access control
- Integrates with project-level permissions
- Respects offering availability rules

## Performance Considerations

### Transaction Safety

- Uses `@transaction.atomic` for data consistency
- Commits migration execution after successful creation
- Prevents partial state corruption during failures

### Async Execution

- Migration execution happens asynchronously via Celery
- Non-blocking API responses with immediate migration record
- Background processing for time-intensive operations

### Resource Optimization

- Selective network migration reduces unnecessary copying
- Efficient quota aggregation for volume type mappings
- Optimized ancestor traversal in quota calculations

## Use Cases

### 1. Service Provider Migration

Migrate tenants between different OpenStack clouds:

```json
{
  "src_resource": "old-provider-tenant",
  "dst_offering": "new-provider-offering",
  "mappings": {
    "volume_types": [
      {"src_type_uuid": "ssd-old", "dst_type_uuid": "ssd-new"}
    ]
  }
}
```

### 2. Development Environment Replication

Copy production tenant to development environment:

```json
{
  "src_resource": "prod-tenant",
  "dst_offering": "dev-offering",
  "name": "Development Environment",
  "mappings": {
    "subnets": [
      {"src_cidr": "10.0.1.0/24", "dst_cidr": "192.168.1.0/24"}
    ]
  }
}
```

### 3. Disaster Recovery Setup

Replicate critical infrastructure with port synchronization:

```json
{
  "src_resource": "primary-tenant",
  "dst_offering": "dr-offering",
  "mappings": {
    "sync_instance_ports": true,
    "networks": ["critical-network-uuid"]
  }
}
```

## Limitations and Considerations

### Current Limitations

- **Instance Migration**: Does not migrate actual VM instances (infrastructure only)
- **Volume Data**: Does not copy volume data (structure only)
- **Floating IPs**: External network connections not fully replicated
- **Custom Metadata**: Some OpenStack metadata may not be preserved

### Security Considerations

- **Credential Generation**: New tenant gets fresh random credentials
- **Network Isolation**: Maintains network isolation in destination environment
- **Permission Boundaries**: Respects Waldur's permission system throughout

### Planning Considerations

- **IP Address Conflicts**: Plan subnet mappings to avoid IP conflicts
- **Volume Type Availability**: Ensure destination volume types exist
- **Quota Limits**: Verify destination environment can accommodate quotas
- **Network Dependencies**: Consider external network connectivity requirements

## Testing

The plugin includes comprehensive test coverage:

### Migration Tests (`tests/test_migration.py`)

- **Basic Migration**: Validates complete tenant migration workflow
- **Network Selection**: Tests selective network migration
- **Volume Type Mapping**: Verifies quota aggregation across type mappings
- **Security Group Replication**: Ensures proper rule and reference handling
- **Error Handling**: Tests graceful failure scenarios

### Port Task Tests

- **Port Creation**: Validates successful port replication
- **Error Recovery**: Tests handling of missing dependencies
- **Security Group Association**: Verifies proper security group mapping

## Configuration

### App Registration (`apps.py:5`)

```python
class OpenStackReplicationConfig(AppConfig):
    name = "waldur_openstack_replication"

    def ready(self):
        # Register migration state change handler
        post_save.connect(handle_migration_post_save, sender=Migration)
```

### URL Configuration (`urls.py:4`)

```python
def register_in(router):
    router.register(r"openstack-migrations", MigrationViewSet,
                   basename="openstack-migrations")
```

### Extension Integration (`extension.py:4`)

```python
class OpenStackReplicationExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_openstack_replication"
```

## Best Practices

### Migration Planning

1. **Pre-Migration Analysis**: Review source tenant configuration
2. **Quota Verification**: Ensure destination has sufficient quotas
3. **Network Planning**: Design subnet mappings to avoid conflicts
4. **Volume Type Mapping**: Map storage types based on performance requirements

### Execution Guidelines

1. **Test Migrations**: Perform test migrations before production
2. **Selective Migration**: Use network selection for large tenants
3. **Monitor Progress**: Track migration state through API
4. **Post-Migration Validation**: Verify all components migrated correctly

### Troubleshooting

1. **Check Logs**: Review migration error messages and tracebacks
2. **Validate Permissions**: Ensure proper access to source and destination
3. **Verify Dependencies**: Confirm all required resources exist
4. **Resource Cleanup**: Clean up failed migrations manually if needed
