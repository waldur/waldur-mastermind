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

## Best Practices

1. **Always use `uuid` for API operations** - it's the stable public identifier
2. **Use `backend_id` for backend system integration** - direct mapping to external UUIDs
3. **Leverage scope relationships** - access the underlying resource through `resource.scope`
4. **Maintain identifier consistency** - ensure `backend_id` matches the scope object's identifiers
5. **Handle effective_id for federation** - check this field when dealing with remote resources
