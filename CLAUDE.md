# Waldur Development Guide for Claude

## Quick Start

This is a Django-based cloud orchestration platform. When working on this codebase:

1. **Study existing patterns first** - Find 3 similar implementations before coding
2. **Follow TDD** - Write tests first, then implement
3. **Use established patterns** - Don't reinvent what already exists
4. **Stop after 3 attempts** - If stuck, document and try different approach

## Critical Rules

**NEVER**:

- Use `--no-verify` to bypass commit hooks
- Disable tests instead of fixing them
- Commit code that doesn't compile
- Make assumptions - verify with existing code
- Import modules inside functions - all imports must be at the top of the file

**ALWAYS**:

- Use `permission_factory` for ViewSet permissions
- Use `SlugRelatedField(slug_field="uuid")` for relationships
- Test actual system behavior, not assumptions
- Run tests and linters before committing

## Key Waldur Patterns

### Permissions

```python
# Use permission_factory, not manual checks
list_permissions = [permission_factory(PermissionEnum.VIEW_RESOURCE)]
```

### Serializers

```python
# Use SlugRelatedField for UUIDs
project = serializers.SlugRelatedField(slug_field="uuid", queryset=Project.objects.all())

# CRITICAL: Nullable FKs MUST use allow_null=True on SlugRelatedField
# Without it, the OpenAPI spec won't mark the field as nullable,
# and auto-generated SDK clients will crash on null values (e.g. UUID(None))
created_by = serializers.SlugRelatedField(slug_field="uuid", read_only=True, allow_null=True)
```

### Testing

```python
# Use established fixtures
fixture = fixtures.ProjectFixture()
# Use real roles
role = CustomerRole.SUPPORT  # Not MANAGER (doesn't exist)
```

### Demo Presets

Demo presets are JSON files in `src/waldur_mastermind/marketplace/demo_presets/presets/` that define complete marketplace ecosystems for testing and demos.

**UUID Format Rules** (CRITICAL):

- UUIDs must be **exactly 32 hexadecimal characters** (0-9, a-f only)
- **NO hyphens** - use continuous string format
- **NO letters g-z** - these are not valid hex characters
- All `*_uuid` reference fields must match the referenced entity's UUID exactly

```python
# CORRECT UUID format
"uuid": "afc00000000000000000000000000001"  # 32 hex chars

# WRONG - contains non-hex letters
"uuid": "afk00000000000000000000000000001"  # 'k' is not hex
"uuid": "af3plan0000000000000000000000001"  # 'p', 'l', 'n' are not hex

# WRONG - wrong length
"uuid": "afc0000000000000000000000000001"   # 31 chars (missing one)
```

**Reference Consistency**: When referencing entities via `*_uuid` fields (e.g., `customer_uuid`, `offering_uuid`, `plan_uuid`), ensure the UUID exactly matches the target entity's `uuid` field.

**Commands**:

```bash
# List available presets
waldur demo_presets list

# Load a preset (destructive - clears existing data)
DJANGO_SETTINGS_MODULE=waldur_core.server.test_settings_local waldur demo_presets load <name> -y

# Dry run (preview without changes)
waldur demo_presets load <name> --dry-run
```

**Generating Billing Data**:

Use the billing data generator script to add realistic invoices, credits, and usage data to presets:

```bash
# Generate 12 months of billing data (invoices, credits, usages)
python scripts/generate_preset_billing_data.py src/waldur_mastermind/marketplace/demo_presets/presets/<preset>.json

# Generate with specific months and reproducible seed
python scripts/generate_preset_billing_data.py preset.json --months 6 --seed 42

# Save to a different file
python scripts/generate_preset_billing_data.py input.json --output output_with_billing.json
```

The script generates:

- Invoices (monthly for each consuming customer)
- Invoice items (per-resource cost breakdown)
- Customer credits (organization-level allocations)
- Project credits (project-level allocations)
- Component usages (resource usage with growth trends)
- Component user usages (per-user breakdown for reporting)

## REST API Development (DRF)

### ViewSet Structure

```python
class MyViewSet(ActionsViewSet):
    queryset = MyModel.objects.all()
    serializer_class = MySerializer
    filterset_class = MyFilter
    lookup_field = "uuid"

    # Permissions per action
    list_permissions = [permission_factory(PermissionEnum.VIEW_X)]
    create_permissions = [permission_factory(PermissionEnum.CREATE_X)]

    # Disable unwanted actions
    disabled_actions = ["destroy"]
```

### Custom Actions

**ALWAYS use serializers** for ViewSet actions - for both input validation and output schema generation.
This ensures proper OpenAPI documentation and consistent API contracts.

```python
# Define serializers for your action responses
class MyActionResponseSerializer(serializers.Serializer):
    """Serializer for MyAction response - used for OpenAPI schema."""
    field_name = serializers.CharField()
    count = serializers.IntegerField()

# In the ViewSet:
@extend_schema(
    summary="Perform my action",
    responses={200: MyActionResponseSerializer(many=True)},
    description="Description for OpenAPI docs.",
)
@action(detail=True, methods=["post"])
def my_action(self, request, uuid=None):
    """Docstring becomes OpenAPI description."""
    instance = self.get_object()
    serializer = MyActionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    # ... implementation
    response_data = [{"field_name": "value", "count": 42}]
    response_serializer = MyActionResponseSerializer(response_data, many=True)
    return Response(response_serializer.data)

# Define permissions for custom action
my_action_permissions = [permission_factory(PermissionEnum.ACTION_X)]
# Link serializer class for the action
my_action_serializer_class = MyActionSerializer
```

### OpenAPI Schema Customization

**Quick decision guide:**

- Endpoint-specific parameters → `@extend_schema` decorator
- Custom field types → Extension in `openapi_extensions.py`
- Hide endpoints → `disabled_actions` list on ViewSet
- Schema-wide changes → Hook in `schema_hooks.py`

```python
from drf_spectacular.utils import extend_schema, OpenApiParameter

@extend_schema(
    parameters=[
        OpenApiParameter("o", {"type": "string", "enum": ["name", "-name"]},
                        location=OpenApiParameter.QUERY,
                        description="Ordering field"),
    ],
)
@action(detail=True)
def items(self, request, uuid=None):
    ...
```

**Validate schema:** `uv run waldur spectacular --validate`

See `docs/guides/openapi.md` for detailed patterns.

**IMPORTANT: No database access during schema generation**

When customizing serializer `get_fields()` or similar methods, **never access the database
or Constance config** without first checking for schema generation context. Schema generation
runs without a database connection.

The OpenAPI schema should show **all possible fields** that could be returned, not a minimal
set. This allows API consumers to see the maximum possible response shape.

```python
def get_fields(self):
    fields = super().get_fields()

    # ALWAYS check for schema generation FIRST, before any DB/Constance access
    if getattr(self.context.get("view"), "swagger_fake_view", False):
        return fields  # Return ALL fields for OpenAPI schema

    # Now safe to access database or Constance config
    some_config = config.MY_SETTING
    # ... filter fields based on config
```

## Documentation Structure

Detailed guides are in `docs/guides/`:

- **Development Philosophy**: `development-philosophy.md` - Core principles and process
- **Architecture**: `waldur-architecture.md` - Django app structure and patterns
- **Testing Guide**: `waldur-testing-guide.md` - Test writing best practices
- **Code Style**: `waldur-code-style.md` - Formatting and conventions
- **Permissions**: `waldur-permissions.md` - Permission system details
- **Build Commands**: `build-commands.md` - Test/lint/build commands
- **OpenAPI Schema**: `openapi.md` - drf-spectacular customization patterns

## Quick Commands

```bash
# Run tests
DJANGO_SETTINGS_MODULE=waldur_core.server.test_settings_local uv run pytest

# Lint/format
uv run pre-commit run --all-files

# Lint markdown
mdl --style markdownlint-style.rb docs/

# Validate OpenAPI schema
uv run waldur spectacular --validate
```

## Development Workflows

### Extended Thinking Mode

Use the word "think" to trigger Claude's extended reasoning for complex problems:

- "Think about the best approach to implement user notifications"
- "Think through the database schema changes needed"
- "Think about potential performance implications"

### Test-Driven Development Workflow

1. **Red Phase**: Write failing tests first
2. **Green Phase**: Implement minimal code to pass
3. **Refactor Phase**: Improve code while tests pass
4. **Verify**: Use independent subagent review

### Visual Development (UI Changes)

1. **Provide Context**: Share screenshots or design mockups using drag & drop
2. **Implement Changes**: Use implementation agent for UI modifications
3. **Visual Validation**: Take screenshots of results using browser tools
4. **Iterate**: Compare before/after, iterate 2-3 times for refinement

#### Screenshot Integration Best Practices

- **Before Changes**: Capture current state with `mcp__playwright__browser_take_screenshot`
- **After Changes**: Take new screenshots to compare results
- **Specific Elements**: Screenshot individual components when relevant
- **Multiple Viewports**: Test responsive design with different browser sizes
- **Error States**: Capture error conditions and edge cases

#### Browser Tool Usage

```bash
# Navigate to page
mcp__playwright__browser_navigate --url "http://localhost:8000/admin"

# Take full page screenshot
mcp__playwright__browser_take_screenshot --fullPage true

# Take element screenshot
mcp__playwright__browser_take_screenshot --element "Submit button"

# Resize for responsive testing
mcp__playwright__browser_resize --width 375 --height 667
```

### Jira Issue Workflow

When working on tasks from Jira (e.g., `https://opennode.atlassian.net/browse/WAL-9564`):

#### 1. Analyze the Issue

- Fetch issue details using Atlassian MCP tools
- Determine issue type: **bug** (fix) or **feature/improvement** (other)
- Check if the issue is already resolved or needs work

#### 2. Prepare the Branch

```bash
# Ensure on latest develop branch
git checkout develop
git pull origin develop

# Create appropriate branch
# For bugs:
git checkout -b bug/WAL-XXXX

# For features/improvements:
git checkout -b feature/WAL-XXXX
```

#### 3. Implement Changes

- Follow TDD workflow (tests first)
- Study existing patterns before coding
- Make minimal, focused changes

#### 4. Commit Changes

```bash
# Only add files created/modified as part of this work
git add <specific-files>

# Commit with proper descriptive message, ticket reference at end of first line
git commit -m "Add validation for resource quota limits [WAL-XXXX]"
```

**Important**: Do NOT add unrelated files to the commit.

#### 5. Report to User

Provide a summary including:

- What was analyzed
- What changes were made (files modified/created)
- How to test the changes
- Any follow-up actions needed

### Multi-Agent Coordination

- **Planning**: implementation agent creates feature plan
- **Development**: implementation agent builds code
- **Testing**: test-generator agent creates comprehensive tests
- **Review**: code-reviewer agent validates quality
- **Documentation**: docs-writer agent updates documentation

## Subagents

Specialized subagents are defined in `.claude/agents/` following Claude Code conventions:

- **code-reviewer**: Reviews code for Django best practices and Waldur conventions
- **test-generator**: Generates tests following project patterns
- **implementation**: Implements features using established patterns
- **performance-analyzer**: Analyzes and optimizes performance
- **docs-writer**: Creates concise, accurate documentation

### Using Subagents

Subagents are automatically available. When you need specialized help, Claude will use the appropriate subagent. You can also explicitly request them:

- "Use the code-reviewer subagent to review src/waldur_core/permissions/"
- "Use the test-generator subagent to create tests for CallViewSet"
- "Use the docs-writer subagent to document the new API endpoints"

See `CLAUDE_SUBAGENT_USAGE.md` for detailed examples and workflows.

## Demo Preset UUIDs

When creating or modifying JSON files in `src/waldur_mastermind/marketplace/demo_presets/presets/`:

### UUID Format Rules

UUIDs must be **exactly 32 hexadecimal characters** (0-9, a-f only). Invalid characters cause import failures:

```text
✓ Valid:   "a3000000000000000000000000000001"
✓ Valid:   "f3000000000000000000000000000005"
✗ Invalid: "p3000000000000000000000000000001"  (p is not hex)
✗ Invalid: "o3000000000000000000000000000001"  (o is not hex)
```

### Entity Prefix Convention

Use hex-safe prefixes to organize UUIDs by entity type:

| Entity Type | Prefix | Example |
|-------------|--------|---------|
| Users | `00` | `00000000000000000000000000000001` |
| Customers | `a3` | `a3000000000000000000000000000001` |
| Projects | `c3` | `c3000000000000000000000000000001` |
| Resources | `d3` | `d3000000000000000000000000000001` |
| Offerings | `f3` | `f3000000000000000000000000000001` |
| Orders | `73` | `73000000000000000000000000000001` |
| Policies | `93` | `93000000000000000000000000000001` |
| Plans | `b3` | `b3000000000000000000000000000001` |
| Categories | `e3` | `e3000000000000000000000000000001` |

### Generating Valid UUIDs

```python
# Generate a valid preset UUID with a prefix
def make_preset_uuid(prefix: str, number: int) -> str:
    """Generate a 32-char hex UUID for presets."""
    suffix = f"{number:030x}"  # 30 hex digits, zero-padded
    return f"{prefix}{suffix}"[-32:]

# Examples:
make_preset_uuid("a3", 1)   # "a3000000000000000000000000000001"
make_preset_uuid("f3", 10)  # "f300000000000000000000000000000a"
```

## Remember

- Incremental progress over big changes
- Learning from existing code over assumptions
- Pragmatic over dogmatic
- Clear intent over clever code

When in doubt, check the existing codebase for patterns.
