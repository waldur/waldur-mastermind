# OpenStack Plugin

## Introduction

The OpenStack plugin for Waldur provides comprehensive integration with OpenStack cloud infrastructure, enabling organizations to manage OpenStack resources through Waldur's unified platform. This plugin acts as a bridge between Waldur's resource management capabilities and OpenStack's Infrastructure-as-a-Service (IaaS) offerings.

### Core Functionality

The plugin enables:

- **Multi-tenant Resource Management**: Create and manage OpenStack projects (tenants) with isolated resources
- **Compute Resource Provisioning**: Deploy and manage virtual machines with full lifecycle control
- **Storage Management**: Provision block storage volumes, create snapshots, and manage backups
- **Network Configuration**: Set up virtual networks, subnets, routers, and security policies
- **Quota Management**: Synchronize and enforce resource quotas between Waldur and OpenStack
- **Cross-tenant Resource Sharing**: Share networks between tenants using RBAC policies
- **Automated Resource Discovery**: Import existing OpenStack resources into Waldur
- **Console Access**: Provide direct console access to virtual machines

## Supported Operations by OpenStack Service

### Keystone (Identity Service)

| Operation | Description | API Endpoint |
|-----------|-------------|--------------|
| Tenant Creation | Create new OpenStack projects/tenants | `POST /api/openstack-tenants/` |
| Tenant Deletion | Remove OpenStack projects | `DELETE /api/openstack-tenants/{uuid}/` |
| Authentication | Manage tenant credentials | Handled internally |
| Quota Retrieval | Fetch tenant quotas | `GET /api/openstack-tenants/{uuid}/quotas/` |
| Quota Update | Modify tenant quotas | `POST /api/openstack-tenants/{uuid}/set_quotas/` |

### Nova (Compute Service)

| Operation | Description | API Endpoint |
|-----------|-------------|--------------|
| **Instances** | | |
| Create Instance | Launch new virtual machines | `POST /api/openstack-instances/` |
| Delete Instance | Terminate virtual machines | `DELETE /api/openstack-instances/{uuid}/` |
| Start Instance | Power on virtual machines | `POST /api/openstack-instances/{uuid}/start/` |
| Stop Instance | Power off virtual machines | `POST /api/openstack-instances/{uuid}/stop/` |
| Restart Instance | Reboot virtual machines | `POST /api/openstack-instances/{uuid}/restart/` |
| Resize Instance | Change instance flavor | `POST /api/openstack-instances/{uuid}/change_flavor/` |
| Console Access | Get VNC console URL | `POST /api/openstack-instances/{uuid}/console/` |
| Attach Volume | Connect storage to instance | `POST /api/openstack-instances/{uuid}/attach_volume/` |
| Detach Volume | Disconnect storage from instance | `POST /api/openstack-instances/{uuid}/detach_volume/` |
| Assign Floating IP | Attach public IP | `POST /api/openstack-instances/{uuid}/assign_floating_ip/` |
| **Flavors** | | |
| List Flavors | Get available VM sizes | `GET /api/openstack-flavors/` |
| Import Flavors | Sync flavors from backend | `POST /api/openstack-tenants/{uuid}/pull_flavors/` |
| **Images** | | |
| List Images | Get available OS images | `GET /api/openstack-images/` |
| Import Images | Sync images from backend | `POST /api/openstack-tenants/{uuid}/pull_images/` |
| **Server Groups** | | |
| Create Server Group | Set up affinity policies | `POST /api/openstack-server-groups/` |
| Delete Server Group | Remove affinity policies | `DELETE /api/openstack-server-groups/{uuid}/` |
| **Availability Zones** | | |
| List AZs | Get compute availability zones | `GET /api/openstack-instance-availability-zones/` |

### Cinder (Block Storage Service)

| Operation | Description | API Endpoint |
|-----------|-------------|--------------|
| **Volumes** | | |
| Create Volume | Provision block storage | `POST /api/openstack-volumes/` |
| Delete Volume | Remove block storage | `DELETE /api/openstack-volumes/{uuid}/` |
| Extend Volume | Increase volume size | `POST /api/openstack-volumes/{uuid}/extend/` |
| Attach to Instance | Connect volume to VM | `POST /api/openstack-volumes/{uuid}/attach/` |
| Detach from Instance | Disconnect volume from VM | `POST /api/openstack-volumes/{uuid}/detach/` |
| Create from Snapshot | Restore volume from snapshot | `POST /api/openstack-volumes/{uuid}/create_from_snapshot/` |
| **Snapshots** | | |
| Create Snapshot | Create volume snapshot | `POST /api/openstack-snapshots/` |
| Delete Snapshot | Remove snapshot | `DELETE /api/openstack-snapshots/{uuid}/` |
| Restore Snapshot | Create volume from snapshot | `POST /api/openstack-snapshots/{uuid}/restore/` |
| **Volume Types** | | |
| List Volume Types | Get storage types (SSD/HDD) | `GET /api/openstack-volume-types/` |
| Import Volume Types | Sync types from backend | `POST /api/openstack-tenants/{uuid}/pull_volume_types/` |
| **Backups** | | |
| Create Backup | Create volume backup | `POST /api/openstack-backups/` |
| Delete Backup | Remove backup | `DELETE /api/openstack-backups/{uuid}/` |
| Restore Backup | Restore volume from backup | `POST /api/openstack-backups/{uuid}/restore/` |

### Neutron (Networking Service)

| Operation | Description | API Endpoint |
|-----------|-------------|--------------|
| **Networks** | | |
| Create Network | Set up virtual network | `POST /api/openstack-networks/` |
| Delete Network | Remove virtual network | `DELETE /api/openstack-networks/{uuid}/` |
| Update Network | Modify network properties | `PATCH /api/openstack-networks/{uuid}/` |
| **Subnets** | | |
| Create Subnet | Define IP address pool | `POST /api/openstack-subnets/` |
| Delete Subnet | Remove subnet | `DELETE /api/openstack-subnets/{uuid}/` |
| Update Subnet | Modify subnet configuration | `PATCH /api/openstack-subnets/{uuid}/` |
| **Routers** | | |
| Create Router | Set up network router | `POST /api/openstack-routers/` |
| Delete Router | Remove router | `DELETE /api/openstack-routers/{uuid}/` |
| Add Interface | Connect subnet to router | `POST /api/openstack-routers/{uuid}/add_interface/` |
| Remove Interface | Disconnect subnet from router | `POST /api/openstack-routers/{uuid}/remove_interface/` |
| Set Gateway | Configure external gateway | `POST /api/openstack-routers/{uuid}/set_gateway/` |
| **Ports** | | |
| Create Port | Create network interface | `POST /api/openstack-ports/` |
| Delete Port | Remove network interface | `DELETE /api/openstack-ports/{uuid}/` |
| Update Port | Modify port configuration | `PATCH /api/openstack-ports/{uuid}/` |
| **Floating IPs** | | |
| Allocate Floating IP | Reserve public IP | `POST /api/openstack-floating-ips/` |
| Release Floating IP | Release public IP | `DELETE /api/openstack-floating-ips/{uuid}/` |
| Associate Floating IP | Attach to instance | `POST /api/openstack-floating-ips/{uuid}/assign/` |
| Disassociate Floating IP | Detach from instance | `POST /api/openstack-floating-ips/{uuid}/unassign/` |
| **Security Groups** | | |
| Create Security Group | Set up firewall rules | `POST /api/openstack-sgp/` |
| Delete Security Group | Remove firewall rules | `DELETE /api/openstack-sgp/{uuid}/` |
| Add Rule | Create firewall rule | `POST /api/openstack-sgp/{uuid}/rules/` |
| Remove Rule | Delete firewall rule | `DELETE /api/openstack-sgp/{uuid}/rules/{rule_id}/` |
| **RBAC Policies** | | |
| Create RBAC Policy | Share network between tenants | `POST /api/openstack-network-rbac-policies/` |
| List RBAC Policies | View sharing policies | `GET /api/openstack-network-rbac-policies/` |

### Glance (Image Service)

| Operation | Description | API Endpoint |
|-----------|-------------|--------------|
| List Images | Get available images | `GET /api/openstack-images/` |
| Import Images | Sync images from Glance | Handled via tenant sync |
| Image Metadata | Get image properties | Included in image list |

## Network Requirements

### Required Network Connectivity

The following table outlines the network ports and protocols required for Waldur to communicate with OpenStack services:

| Service | Port | Protocol | Direction | Description | Required |
|---------|------|----------|-----------|-------------|----------|
| **Keystone (Identity)** | 5000 | HTTPS/HTTP | Outbound | Public API endpoint for authentication | ✅ Yes |
| **Keystone (Admin)** | 35357 | HTTPS/HTTP | Outbound | Admin API endpoint (deprecated in newer versions) | ⚠️ Version dependent |
| **Nova (Compute)** | 8774 | HTTPS/HTTP | Outbound | Compute API for instance management | ✅ Yes |
| **Cinder (Block Storage)** | 8776 | HTTPS/HTTP | Outbound | Volume API for storage management | ✅ Yes |
| **Neutron (Networking)** | 9696 | HTTPS/HTTP | Outbound | Network API for networking operations | ✅ Yes |
| **Glance (Images)** | 9292 | HTTPS/HTTP | Outbound | Image API for image management | ✅ Yes |
| **Nova VNC Console** | 6080 | HTTPS/HTTP | Outbound | VNC console proxy for instance access | ⚠️ Optional* |
| **Horizon Dashboard** | 80/443 | HTTPS/HTTP | Outbound | Generate links to OpenStack web UI for users | ⚠️ Optional** |

\* *Required only if console access feature is enabled*
\** *Only used to generate dashboard URLs for user convenience; not required for API operations*

### Network Configuration Notes

1. **SSL/TLS Requirements**:
  - HTTPS is strongly recommended for all API communications
  - Self-signed certificates are supported but require configuration
  - Certificate validation can be disabled for testing (not recommended for production)

2. **Firewall Considerations**:
  - All connections are initiated from Waldur to OpenStack (outbound only)
  - No inbound connections to Waldur are required from OpenStack
  - Stateful firewall rules should allow return traffic

3. **API Endpoint Discovery**:
  - Waldur uses Keystone service catalog for endpoint discovery
  - Only the Keystone endpoint needs to be explicitly configured
  - Other service endpoints are automatically discovered from the service catalog

4. **Network Latency**:
  - API timeout: 60 seconds (configurable)
  - Recommended latency: < 100ms
  - Long-running operations use asynchronous task queues

## Configuration

### Marketplace-Based Configuration

OpenStack integration in Waldur is configured through Marketplace offerings. The plugin provides three offering types for different resource levels:

| Offering Type | Purpose | Resource Scope |
|---------------|---------|----------------|
| `OpenStack.Tenant` | Provision and manage OpenStack projects/tenants | Provider-level |
| `OpenStack.Instance` | Provision virtual machines within a tenant | Tenant-level |
| `OpenStack.Volume` | Provision block storage volumes within a tenant | Tenant-level |

### Configuring an OpenStack Provider

To set up an OpenStack provider, create a Marketplace offering of type `OpenStack.Tenant` with the following configuration:

#### Required Connection Settings

| Parameter | Location | Description | Example |
|-----------|----------|-------------|---------|
| `backend_url` | secret_options | Keystone API endpoint URL | `https://keystone.example.com:5000/v3` |
| `username` | secret_options | Admin account username | `admin` |
| `password` | secret_options | Admin account password | `secure_password` |
| `tenant_name` | secret_options | Admin tenant/project name | `admin` |
| `domain` | secret_options | Keystone domain (v3 only) | `default` |

#### Network Configuration

| Parameter | Location | Description | Required |
|-----------|----------|-------------|----------|
| `external_network_id` | secret_options | UUID of external network for floating IPs | Yes |
| `default_internal_network_mtu` | plugin_options | MTU for tenant internal networks (68-9000) | No |
| `ipv4_external_ip_mapping` | secret_options | NAT mapping for floating IPs | No |

#### Optional Settings

| Parameter | Location | Description | Default |
|-----------|----------|-------------|---------|
| `access_url` | options | Horizon dashboard URL for user links | Generated from backend_url |
| `verify_ssl` | options | Verify SSL certificates | `true` |
| `availability_zone` | options | Default availability zone | `nova` |
| `storage_mode` | plugin_options | Storage quota mode (`fixed` or `dynamic`) | `fixed` |

### Storage Modes

The plugin supports two storage quota modes:

| Mode | Description | Use Case |
|------|-------------|----------|
| `fixed` | Single storage quota shared by all volume types | Simple environments with uniform storage |
| `dynamic` | Separate quotas per volume type (SSD, HDD, etc.) | Environments with tiered storage offerings |

When using dynamic storage mode, volume types are automatically synchronized from OpenStack and created as offering components.

### Resource Components

OpenStack tenant offerings include the following billable components:

| Component Type | Description | Unit | Default Limit |
|----------------|-------------|------|---------------|
| `cores` | CPU cores | Count | 20 |
| `ram` | Memory | MB | 51200 |
| `storage` | Block storage (fixed mode) | MB | 1048576 |
| `volume_type_*` | Per-type storage (dynamic mode) | MB | Varies |

### Automated Private Offerings

When `AUTOMATICALLY_CREATE_PRIVATE_OFFERING` is enabled in settings, the plugin automatically creates private offerings for instances and volumes when a tenant is provisioned. This allows tenant users to order resources through the Marketplace interface.

### Quota Mapping

OpenStack quotas are automatically synchronized with Waldur quotas:

| OpenStack Quota | Waldur Quota | Default Limit |
|-----------------|--------------|---------------|
| cores | vcpu | 20 |
| ram | ram | 51200 MB |
| instances | instances | 30 |
| volumes | volumes | 50 |
| gigabytes | storage | 1024 GB |
| snapshots | snapshots | 50 |
| security_groups | security_group_count | 100 |
| security_group_rules | security_group_rule_count | 100 |
| floatingip | floating_ip_count | 50 |
| network | network_count | 10 |
| subnet | subnet_count | 10 |
| port | port_count | Unlimited |

## Scheduled Tasks

The plugin runs the following automated tasks:

| Task | Schedule | Purpose |
|------|----------|---------|
| Pull Quotas | Every 12 hours | Synchronize quotas with OpenStack |
| Pull Resources | Every 1 hour | Update resource states |
| Pull Sub-resources | Every 2 hours | Sync networks, subnets, ports |
| Pull Properties | Every 24 hours | Update flavors, images, volume types |
| Cleanup Stuck Resources | Every 1 hour | Mark stuck resources as erred |
| Delete Expired Backups | Every 10 minutes | Remove backups past retention |
| Delete Expired Snapshots | Every 10 minutes | Remove snapshots past retention |

## Security Considerations

1. **Credential Management**:
  - Service account credentials are stored in database `secret_options` field
  - Per-tenant credentials are auto-generated using random passwords
  - Tenant credentials visibility can be controlled via `TENANT_CREDENTIALS_VISIBLE` setting
  - SSH keys are automatically distributed to tenants based on user permissions

2. **Network Security**:
  - Security groups provide instance-level firewalling
  - Default deny-all policy for new security groups
  - RBAC policies control cross-tenant network resource sharing
  - External IP mapping supports NAT scenarios

3. **Audit Logging**:
  - All operations are logged with user attribution
  - Resource state changes tracked in event log
  - Failed operations logged with error details
  - Quota changes trigger audit events

## Troubleshooting

### Common Issues

1. **Connection Timeouts**:
  - Verify network connectivity to Keystone endpoint
  - Check firewall rules for required ports
  - Validate SSL certificates if using HTTPS

2. **Authentication Failures**:
  - Verify service account credentials
  - Check domain configuration for Keystone v3
  - Ensure service account has admin privileges

3. **Quota Synchronization Issues**:
  - Check OpenStack policy files for quota permissions
  - Verify nova, cinder, and neutron quota drivers
  - Review background task logs

4. **Resource State Mismatches**:
  - Trigger manual pull operation
  - Check OpenStack service status
  - Review executor task logs

## Marketplace Integration

The OpenStack plugin integrates with Waldur Marketplace through the `marketplace_openstack` module, providing:

- Simplified instance and volume ordering
- Component-based pricing (CPU, RAM, Storage)
- Automated provisioning workflows
- Resource metadata management
- Usage tracking for billing

## API Reference

For detailed API documentation, refer to the Waldur API schema at `/api/schema/` with the OpenStack plugin enabled.
