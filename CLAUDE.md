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
```

### Testing

```python
# Use established fixtures
fixture = fixtures.ProjectFixture()
# Use real roles
role = CustomerRole.SUPPORT  # Not MANAGER (doesn't exist)
```

## Documentation Structure

Detailed guides are in `docs/guides/`:

- **Development Philosophy**: `development-philosophy.md` - Core principles and process
- **Architecture**: `waldur-architecture.md` - Django app structure and patterns
- **Testing Guide**: `waldur-testing-guide.md` - Test writing best practices
- **Code Style**: `waldur-code-style.md` - Formatting and conventions
- **Permissions**: `waldur-permissions.md` - Permission system details
- **Build Commands**: `build-commands.md` - Test/lint/build commands

## Quick Commands

```bash
# Run tests
DJANGO_SETTINGS_MODULE=waldur_core.server.my_test_settings uv run pytest

# Lint/format
uv run pre-commit run --all-files

# Lint markdown
mdl --style markdownlint-style.rb docs/
```

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

## Remember

- Incremental progress over big changes
- Learning from existing code over assumptions
- Pragmatic over dogmatic
- Clear intent over clever code

When in doubt, check the existing codebase for patterns.
