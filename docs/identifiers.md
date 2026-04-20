# Marketplace Resource Identifiers

This document describes the identifier fields used in Waldur marketplace Resources and provides examples of how they work with OpenStack instances and volumes.

## Identifier Fields Overview

The marketplace Resource model (`src/waldur_mastermind/marketplace/models.py:1255`) provides a comprehensive identifier system through various mixins and direct fields:

| Field | Type | Source | Purpose | Uniqueness | Notes |
|-------|------|--------|---------|------------|--------|
| `id` | `int` | Django AutoField | Primary key | Globally unique | Auto-incremented database ID |
| `uuid` | `UUID` | UuidMixin | External API identifier | Globally unique | Used in REST API endpoints |
| `name` | `CharField(150)` | NameMixin | Human-readable identifier | Per scope | Display name with validation |
| `slug` | `SlugField` | SlugMixin | URL-friendly identifier | Per scope | Auto-generated from name |
| `backend_id` | `CharField(255)` | BackendMixin | Backend system identifier | Per backend | External system mapping |
| `effective_id` | `CharField(255)` | Resource | Remote Waldur identifier | Per remote system | Used for remote provisioning |
| `content_type` | `ForeignKey` | ScopeMixin | Generic relation type | N/A | Part of generic foreign key |
| `object_id` | `PositiveIntegerField` | ScopeMixin | Generic relation ID | N/A | Part of generic foreign key |

## Identifier Categories

### Internal Identification

- **`id`**: Primary key for database operations
- **`uuid`**: Public API identifier, used in REST endpoints like `/api/marketplace-resources/{uuid}/`

### Human Identification

- **`name`**: User-friendly display name
- **`slug`**: URL-safe version of name for web interfaces

### External Integration

- **`backend_id`**: Maps to external system identifiers (e.g., OpenStack instance UUID)
- **`effective_id`**: Used when resource is provisioned through remote Waldur instances

### Scope Linking

- **`content_type`/`object_id`**: Generic foreign key linking to the underlying resource implementation

## Examples with OpenStack Resources

### OpenStack Instance Resource

When a marketplace Resource represents an OpenStack virtual machine (offering type `"OpenStack.Instance"`):

```python
# Marketplace Resource identifiers
resource.id = 1234                                    # Database primary key
resource.uuid = "a1b2c3d4-e5f6-7890-abcd-1234567890ef"  # API identifier
resource.name = "web-server-prod"                     # Human-readable name
resource.slug = "web-server-prod"                     # URL-safe slug
resource.backend_id = "f9e8d7c6-b5a4-3210-9876-fedcba098765"  # OpenStack instance UUID
resource.effective_id = ""                            # Empty (local provisioning)
resource.offering.type = "OpenStack.Instance"         # Offering type constant

# Scope linking to OpenStack instance (src/waldur_openstack/models.py:1149)
from django.contrib.contenttypes.models import ContentType
from waldur_openstack.models import Instance

resource.content_type = ContentType.objects.get_for_model(Instance)
resource.object_id = 5678                             # Points to openstack.Instance.id
resource.scope = Instance.objects.get(id=5678)        # The actual OpenStack instance

# The linked OpenStack instance (inherits from structure.VirtualMachine)
instance = resource.scope
instance.uuid = "f9e8d7c6-b5a4-3210-9876-fedcba098765"  # Same as resource.backend_id
instance.backend_id = "f9e8d7c6-b5a4-3210-9876-fedcba098765"  # OpenStack UUID
instance.name = "web-server-prod"                     # Usually matches resource.name
instance.cores = 2                                    # VM specifications
instance.ram = 4096                                   # Memory in MiB
instance.disk = 20480                                 # Disk in MiB
```

### OpenStack Volume Resource

When a marketplace Resource represents an OpenStack volume (offering type `"OpenStack.Volume"`):

```python
# Marketplace Resource identifiers
resource.id = 5678                                    # Database primary key
resource.uuid = "z9y8x7w6-v5u4-3210-stuv-9876543210ab"  # API identifier
resource.name = "database-storage"                    # Human-readable name
resource.slug = "database-storage"                    # URL-safe slug
resource.backend_id = "m1n2o3p4-q5r6-7890-efgh-abcdef123456"  # OpenStack volume UUID
resource.effective_id = ""                            # Empty (local provisioning)
resource.offering.type = "OpenStack.Volume"           # Offering type constant

# Scope linking to OpenStack volume (src/waldur_openstack/models.py:896)
from django.contrib.contenttypes.models import ContentType
from waldur_openstack.models import Volume

resource.content_type = ContentType.objects.get_for_model(Volume)
resource.object_id = 9012                             # Points to openstack.Volume.id
resource.scope = Volume.objects.get(id=9012)          # The actual OpenStack volume

# The linked OpenStack volume (inherits from structure.Storage)
volume = resource.scope
volume.uuid = "m1n2o3p4-q5r6-7890-efgh-abcdef123456"  # Same as resource.backend_id
volume.backend_id = "m1n2o3p4-q5r6-7890-efgh-abcdef123456"  # OpenStack UUID
volume.name = "database-storage"                      # Usually matches resource.name
volume.size = 100                                     # Size in MiB
```

## Identifier Relationships

### API Usage

- REST API endpoints use `uuid`: `/api/marketplace-resources/{uuid}/`

### Backend Synchronization

- `backend_id` stores the OpenStack UUID for API calls to OpenStack
- The linked scope object (Instance/Volume) also stores this UUID in its `backend_id`
- This creates a redundant but necessary mapping for efficient queries

### Remote Provisioning

- `effective_id` is used when resources are provisioned through remote Waldur instances
- For local OpenStack resources, this field remains empty
- Enables federated deployments where one Waldur manages resources on another

## Integration Guidelines

### Slug Uniqueness and History

The `slug` field provides URL-friendly identifiers with specific uniqueness guarantees:

- **Intended to be unique in history** - When generating new slugs, Waldur considers both active and soft-deleted objects
- **Soft deletion safe** - Resources marked as deleted but not hard-deleted are still considered for slug uniqueness
- **Hard deletion risk** - Objects that are hard-deleted (e.g., Customer objects) can result in duplicate slugs in historical data
- **Full historical uniqueness** - Only UUIDs provide complete uniqueness guarantees across the entire system history

```python
# Example slug generation considering soft-deleted resources
def generate_unique_slug(name, model_class):
    base_slug = slugify(name)
    # This check includes soft-deleted objects (deleted=True)
    existing_slugs = model_class.objects.filter(
        slug__startswith=base_slug
    ).values_list('slug', flat=True)
    if base_slug not in existing_slugs:
        return base_slug
    # Generate numbered suffix for uniqueness
    counter = 1
    while f"{base_slug}-{counter}" in existing_slugs:
        counter += 1
    return f"{base_slug}-{counter}"
```

### Project Slug Templates

Organizations can define custom slug templates for their projects via the `project_slug_template` field on the Customer model. When set, new projects auto-generate slugs using the template instead of the default name-based generation.

**Configuration:** Staff users set the template on a Customer via the API:

```http
PATCH /api/customers/{uuid}/
{"project_slug_template": "{customer_slug}-{year}-{counter_padded}"}
```

The field is read-only for non-staff users but visible in API responses.

**Supported placeholders:**

| Placeholder | Description | Example |
|-------------|-------------|---------|
| `{customer_slug}` | Organization slug | `acme` |
| `{project_name}` | Slugified project name | `my-project` |
| `{year}` | Current year (4-digit) | `2026` |
| `{month}` | Current month (2-digit) | `04` |
| `{counter}` | Sequential project number per organization | `3` |
| `{counter_padded}` | Zero-padded 3-digit counter | `003` |

**Behavior:**

- Slug is generated only on project creation when no slug is provided
- If a staff user provides a slug explicitly, it takes precedence over the template
- If the template is invalid or contains unknown placeholders, generation falls back to the default name-based slug
- Counter is scoped per customer (counts existing projects in the organization)
- Uniqueness is enforced globally; a numeric suffix is appended on collision
- The template can be changed at any time, even when projects exist

**Examples:**

| Template | Result (3rd project, customer slug `csc`) |
|----------|----------------------------------------------|
| `{customer_slug}-{counter_padded}` | `csc-003` |
| `{customer_slug}-{year}{month}-{counter_padded}` | `csc-202604-003` |
| `{project_name}-{counter}` | `my-project-3` |
| *(no template set)* | Default: slugified project name, e.g. `my-projec` |

### Backend ID Usage

The `backend_id` field serves as a bridge to external systems with specific characteristics:

- **External system ownership** - Set by external parties, not controlled by Waldur
- **No uniqueness guarantees** - Waldur makes no assumptions about backend_id uniqueness
- **Discovery mechanism** - Used to map Waldur resources to external system identifiers
- **Integration flexibility** - Allows multiple resources to share the same backend_id if the external system requires it

## Integration Examples

### Storage System Path Mapping

A common integration pattern where Waldur resources map to storage system paths:

```python
# Marketplace Resource for storage allocation
resource.uuid = "a1b2c3d4-e5f6-7890-abcd-1234567890ef"
resource.name = "ml-dataset-storage"
resource.slug = "ml-dataset-storage"                    # Used as input for folder name
resource.project.customer.abbreviation = "ACME"        # Customer identifier
resource.project.name = "machine-learning"             # Project identifier
resource.offering.name = "high-performance-storage"    # Storage offering

# Storage system sets backend_id to full path
resource.backend_id = "/storage/customers/ACME/projects/machine-learning/offerings/high-performance-storage/ml-dataset-storage"

# Path construction in storage integration
def generate_storage_path(resource):
    customer_abbr = resource.project.customer.abbreviation
    project_name = slugify(resource.project.name)
    offering_name = slugify(resource.offering.name)
    resource_slug = resource.slug  # Last folder component from slug

    full_path = f"/storage/customers/{customer_abbr}/projects/{project_name}/offerings/{offering_name}/{resource_slug}"

    # Storage system returns this path as backend_id
    return full_path

# Discovery: Find Waldur resource by storage path
def find_resource_by_storage_path(storage_path):
    return Resource.objects.filter(backend_id=storage_path).first()
```

### SLURM Account Integration

Integration with SLURM workload manager where accounts are managed externally:

```python
# Marketplace Resource for SLURM account
resource.uuid = "b2c3d4e5-f6g7-8901-bcde-234567890123"
resource.name = "hpc-compute-account"
resource.slug = "hpc-compute-account"
resource.project.name = "climate-modeling"
resource.project.customer.abbreviation = "UNIV"

# SLURM system creates account with specific naming convention
slurm_account = f"{resource.project.customer.abbreviation}_{resource.project.name}_{resource.slug}"
# Result: "UNIV_climate-modeling_hpc-compute-account"

# SLURM sets backend_id to the account identifier
resource.backend_id = slurm_account

# Integration points
class SlurmIntegration:
    def create_account(self, resource):
        account_name = self.generate_account_name(resource)

        # Create account in SLURM
        result = slurm_api.create_account(
            name=account_name,
            description=resource.name,
            organization=resource.project.customer.name
        )

        # Store SLURM account name as backend_id
        resource.backend_id = account_name
        resource.save()

        return result

    def find_by_slurm_account(self, account_name):
        """Discover Waldur resource by SLURM account name"""
        return Resource.objects.filter(backend_id=account_name).first()

    def sync_account_usage(self, account_name):
        """Sync usage data from SLURM to Waldur"""
        resource = self.find_by_slurm_account(account_name)
        if resource:
            usage_data = slurm_api.get_account_usage(account_name)
            self.update_resource_usage(resource, usage_data)

# Multiple resources can have same backend_id in different contexts
# Example: Different projects using same SLURM partition
resource_1.backend_id = "gpu_partition"  # Project A uses GPU partition
resource_2.backend_id = "gpu_partition"  # Project B uses same GPU partition
```

### Backend ID Discovery Patterns

Common patterns for using backend_id in external integrations:

```python
# Pattern 1: Direct mapping to external resource ID
resource.backend_id = external_system.resource.id

# Pattern 2: Composite identifier for complex systems
resource.backend_id = f"{tenant_id}:{resource_type}:{external_id}"

# Pattern 3: Path-based identifier
resource.backend_id = "/path/to/resource/in/external/system"

# Pattern 4: Multiple resources sharing backend infrastructure
# (e.g., multiple VM resources using same OpenStack flavor)
for resource in vm_resources:
    resource.backend_id = openstack_flavor.uuid  # Same for all similar VMs
```

## Best Practices

1. **Always use `uuid` for API operations** - it's the stable public identifier
2. **Use `backend_id` for backend system integration** - direct mapping to external UUIDs
3. **Leverage scope relationships** - access the underlying resource through `resource.scope`
4. **Maintain identifier consistency** - ensure `backend_id` matches the scope object's identifiers
5. **Handle effective_id for federation** - check this field when dealing with remote resources
6. **Consider slug history** - When using slugs for external paths, account for soft-deletion behavior
7. **Design for backend_id flexibility** - Don't assume uniqueness, allow for shared backend resources
8. **Document discovery patterns** - Clearly define how external systems will query Waldur using backend_id
