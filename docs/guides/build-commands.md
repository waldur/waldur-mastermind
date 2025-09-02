# Build, Test, and Lint Commands

## Development Setup

- **Install dev dependencies**: `uv sync --extra dev`

## Testing Commands

- **Run all tests**: `DJANGO_SETTINGS_MODULE=waldur_core.server.my_test_settings uv run pytest`
- **Run specific module tests**: `DJANGO_SETTINGS_MODULE=waldur_core.server.my_test_settings uv run pytest src/waldur_core/core/tests/test_serializers.py`
- **Run single test**: `DJANGO_SETTINGS_MODULE=waldur_core.server.my_test_settings uv run pytest src/waldur_core/core/tests/test_serializers.py::RestrictedSerializerTest::test_serializer_returns_fields_required_in_request -v`
- **Verbose output**: Add `-v -s` flags for detailed output with print statements

## Code Quality Commands

- **Lint code**: `uv run pre-commit run --all-files`
- **Format code**: `uv run pre-commit run --all-files`
- **Check code style**: `uv run pre-commit run --all-files`

## Markdown Linting

- **Lint docs directory**: `mdl --style markdownlint-style.rb docs/`
- **Lint project docs**: `mdl --style markdownlint-style.rb CLAUDE.md docs/`
- **Lint specific file**: `mdl --style markdownlint-style.rb path/to/file.md`

## Claude Code Subagent Validation

- **Validate subagents**: `.claude/validate-agents.sh`

### Common MD007 Issues and Fixes

- **Use exactly 2 spaces** for nested list items (configured in markdownlint-style.rb)
- **Be consistent** - if parent uses `*` or `-`, all children at same level should use same indentation
- **Table section headers** need empty cells to match column count: `| **Section** | | |`
- **Fix incrementally** - ensure ALL items at the same nesting level use identical spacing

### Debugging Markdown Issues

- Use `sed -n 'Xp' file | hexdump -C` to see exact spacing (look for `20 20` = 2 spaces)
- Run `mdl --verbose` to see which specific rule is processing
- Check markdownlint-style.rb for custom rule configurations
