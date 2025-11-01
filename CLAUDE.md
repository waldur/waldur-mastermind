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

## Remember

- Incremental progress over big changes
- Learning from existing code over assumptions
- Pragmatic over dogmatic
- Clear intent over clever code

When in doubt, check the existing codebase for patterns.
