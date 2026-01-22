# Offering Tags

Tags provide a flexible way to categorize and filter offerings in the Waldur marketplace. Unlike categories (which are hierarchical and managed by administrators), tags are free-form labels that service providers can create and assign to their offerings.

## Overview

Tags enable:

- **Discovery**: Users can filter offerings by tags to find relevant services
- **Organization**: Service providers can group related offerings across categories
- **Flexibility**: Tags can be created on-demand without administrator intervention

## Permission Model

Tags have a permission model that balances flexibility with control:

| Action | Staff | Service Provider (own tag) | Service Provider (other's tag) | Regular User |
|--------|-------|----------------------------|-------------------------------|--------------|
| List/Retrieve tags | Yes | Yes | Yes | Yes |
| Create tag | Yes | Yes | Yes | No |
| Update tag | Yes | Yes | No | No |
| Delete tag | Yes | Yes | No | No |
| Add tag to offering | Yes | Yes (own offering) | Yes (own offering) | No |

### Key Rules

1. **Anyone authenticated can view tags** - Tags are visible to all authenticated users
2. **Service providers can create tags** - Users belonging to organizations with a ServiceProvider registration can create new tags
3. **Ownership control for modifications** - Only the tag creator (or staff) can update or delete a tag
4. **Tag assignment follows offering permissions** - Users who can edit an offering can assign any existing tag to it

## Tag Model

Each tag has the following attributes:

| Field | Type | Description |
|-------|------|-------------|
| `uuid` | UUID | Unique identifier |
| `name` | String (100 chars) | Unique tag name |
| `description` | Text | Optional description |
| `created` | DateTime | When the tag was created |
| `created_by` | User FK | User who created the tag |

## API Endpoints

### Tag Management

```bash
# List all tags
GET /api/marketplace-tags/

# Create a new tag
POST /api/marketplace-tags/
{
  "name": "hpc",
  "description": "High-performance computing resources"
}

# Get tag details
GET /api/marketplace-tags/{uuid}/

# Update a tag (creator or staff only)
PATCH /api/marketplace-tags/{uuid}/
{
  "description": "Updated description"
}

# Delete a tag (creator or staff only)
DELETE /api/marketplace-tags/{uuid}/
```

### Assigning Tags to Offerings

```bash
# Set tags on an offering (replaces all existing tags)
POST /api/marketplace-provider-offerings/{uuid}/update_tags/
{
  "tags": [
    "tag-uuid-1",
    "tag-uuid-2"
  ]
}

# Remove all tags from an offering
POST /api/marketplace-provider-offerings/{uuid}/delete_tags/
```

### Filtering Offerings by Tags

```bash
# Filter by tag UUID (single)
GET /api/marketplace-provider-offerings/?tag={tag-uuid}

# Filter by tag name (single)
GET /api/marketplace-provider-offerings/?tag_name=hpc

# Multiple tags with OR logic (offerings with ANY of these tags)
GET /api/marketplace-provider-offerings/?tag={uuid1}&tag={uuid2}
GET /api/marketplace-provider-offerings/?tag_name=hpc&tag_name=gpu

# Multiple tags with AND logic (offerings with ALL of these tags)
GET /api/marketplace-provider-offerings/?tags_and={uuid1},{uuid2}
GET /api/marketplace-provider-offerings/?tag_names_and=hpc,gpu
```

## Response Format

### Tag List/Detail Response

```json
{
  "url": "http://example.com/api/marketplace-tags/abc123/",
  "uuid": "abc123...",
  "name": "hpc",
  "description": "High-performance computing resources",
  "offering_count": 5,
  "created": "2024-01-15T10:30:00Z",
  "created_by_username": "john.doe",
  "created_by_full_name": "John Doe"
}
```

### Offering Response with Tags

```json
{
  "uuid": "offering-uuid...",
  "name": "HPC Cluster Access",
  "tags": [
    {"uuid": "tag-uuid-1", "name": "hpc"},
    {"uuid": "tag-uuid-2", "name": "gpu"}
  ]
}
```

## Offering Count Visibility

The `offering_count` field in tag responses is filtered based on user permissions:

| User Type | Visible Offerings |
|-----------|-------------------|
| Staff/Support | All offerings with this tag |
| Service Provider | Own offerings (any state) + Other offerings in ACTIVE, PAUSED, or ARCHIVED states |
| Regular User | Offerings in ACTIVE, PAUSED, or ARCHIVED states |

This ensures service providers don't see competitor's draft offerings in the count.

## Filter Options

### Tag Filter Parameters

| Parameter | Description |
|-----------|-------------|
| `name` | Filter tags by name (case-insensitive contains) |
| `created_by` | Filter tags by creator's UUID |

### Offering Filter Parameters

| Parameter | Description |
|-----------|-------------|
| `tag` | Filter offerings by tag UUID (multiple allowed, OR logic) |
| `tag_name` | Filter offerings by tag name (multiple allowed, OR logic) |
| `tags_and` | Filter offerings by comma-separated tag UUIDs (AND logic) |
| `tag_names_and` | Filter offerings by comma-separated tag names (AND logic, exact match) |

## Use Cases

### 1. Technology Stack Tags

Service providers can tag offerings by technology:

```bash
# Create technology tags
POST /api/marketplace-tags/ {"name": "kubernetes"}
POST /api/marketplace-tags/ {"name": "openstack"}
POST /api/marketplace-tags/ {"name": "slurm"}

# Assign to offerings (using tag UUIDs)
POST /api/marketplace-provider-offerings/{uuid}/update_tags/
{"tags": ["kubernetes-tag-uuid", "slurm-tag-uuid"]}
```

### 2. Capability Tags

Tag offerings by their capabilities:

```bash
POST /api/marketplace-tags/ {"name": "gpu-enabled"}
POST /api/marketplace-tags/ {"name": "high-memory"}
POST /api/marketplace-tags/ {"name": "ssd-storage"}
```

### 3. Compliance Tags

Indicate compliance certifications:

```bash
POST /api/marketplace-tags/ {"name": "gdpr-compliant"}
POST /api/marketplace-tags/ {"name": "iso27001"}
POST /api/marketplace-tags/ {"name": "hipaa"}
```

### 4. Geographic Tags

Tag by data center location:

```bash
POST /api/marketplace-tags/ {"name": "eu-west"}
POST /api/marketplace-tags/ {"name": "us-east"}
POST /api/marketplace-tags/ {"name": "asia-pacific"}
```

## Best Practices

1. **Use lowercase names** - Keep tag names lowercase for consistency
2. **Use hyphens for multi-word tags** - e.g., `high-memory` instead of `high memory`
3. **Keep names concise** - Short, descriptive names work best for filtering
4. **Add descriptions** - Help users understand what the tag represents
5. **Avoid duplicates** - Check existing tags before creating new ones
6. **Coordinate with other providers** - Consistent tagging across providers improves discovery
