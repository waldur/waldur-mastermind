# Waldur Code Style Guide

## Import Organization

- **Standards**: Use `isort` with sections: future, standard-library, first-party, third-party, local-folder
- **Placement**: ALWAYS place all imports at the top of the module
- **Inline Imports**: NEVER use inline imports within functions or methods (except for dynamic imports when absolutely necessary)

## Formatting

- **Formatter**: Follow ruff formatting rules
- **Line Length**: Line length restrictions are ignored (E501)
- **Indentation**: Use 4 spaces, never tabs

## Type Hints

- **Version**: Python 3.10+ compatibility
- **Usage**: Use type hints where possible
- **Modern Syntax**: Use `dict[str, str]` instead of `Dict[str, str]`

## Naming Conventions

- **Functions/Variables**: Use snake_case
- **Classes**: Use CamelCase
- **Constants**: Use UPPER_SNAKE_CASE
- **Django Models**: Follow Django conventions
- **Private**: Prefix with underscore for internal use

## Error Handling

- **Core Exceptions**: Use exceptions from `waldur_core.core.exceptions`
- **Logging**: Add appropriate logging for errors
- **Context**: Include debugging context in error messages
- **Re-raising**: Preserve original traceback when re-raising

## Documentation

- **Docstrings**: Required for public methods and classes
- **Comments**: Avoid unnecessary comments - code should be self-documenting
- **TODO**: Use `# TODO:` format with description and owner

## Testing

- **Unit Tests**: For complex functions
- **API Tests**: For all endpoints
- **Directory Structure**: Follow existing test directory structure
- **Naming**: Test files should match module names with `test_` prefix

## Serializers

- **REST Conventions**: Follow Django REST Framework patterns
- **Relationships**: Use HyperlinkedRelatedField for relationships
- **Query Optimization**: Implement `eager_load()` methods
- **UUID Fields**: ALWAYS use `SlugRelatedField(slug_field="uuid")` instead of PrimaryKeyRelatedField

## API Schema

- **Type Annotations**: Use modern syntax (e.g., `dict[str, str]`)
- **Response Documentation**: Avoid verbose dictionary literals
- **Simple Actions**: Omit unnecessary `responses={status.HTTP_200_OK: None}`
- **OpenAPI**: Use `@extend_schema` decorators appropriately

## Markdown Documentation

- **Linting**: ALL markdown must pass `mdl --style markdownlint-style.rb <path>`
- **List Indentation**: Use exactly 2 spaces for nested items
- **Consistency**: Maintain consistent formatting throughout

## API Design Consistency

- **Default Parameters**: Choose defaults that match most common use case
- **Error Hierarchy**:
  1. Configuration errors (AttributeError) for invalid code/setup
  2. Permission errors (PermissionDenied) for access control
  3. Validation errors for user input
- **Function Signatures**: Document parameter behavior clearly, especially optional parameters

## Django Choices (Enums)

- **Use Django's Built-in Classes**: Always use `models.TextChoices` or `models.IntegerChoices` for enums
- **Syntax**: Define choices using `VALUE = "value", "Label"` for TextChoices or `VALUE = 1, "Label"` for IntegerChoices
- **Location**: Define enums in `enums.py` modules, not in `models.py`
- **Forbidden Patterns**: NEVER create custom enum classes with `.CHOICES` or `.VALUES` attributes

### Example

```python
# Good - in enums.py
from django.db import models

class OrderStates(models.IntegerChoices):
    PENDING = 1, "Pending"
    EXECUTING = 2, "Executing"
    DONE = 3, "Done"
    ERRED = 4, "Erred"

class OfferingTypes(models.TextChoices):
    STANDARD = "standard", "Standard"
    PREMIUM = "premium", "Premium"
```

### Usage in Models

```python
# Good - in models.py
from . import enums

class Order(models.Model):
    state = models.IntegerField(
        choices=enums.OrderStates.choices,  # Use .choices
        default=enums.OrderStates.PENDING
    )
```

### Usage in Serializers and Filters

```python
# Good - accessing enum properties
OrderStates.choices       # Returns tuple of (value, label) pairs
OrderStates.labels        # Returns tuple of labels
OrderStates.values        # Returns tuple of values
OrderStates.names         # Returns tuple of member names

# Bad - deprecated patterns
OrderStates.CHOICES       # DON'T USE - old pattern
OrderStates.VALUES        # DON'T USE - old pattern
```

### OpenAPI Schema

```python
# Good - in openapi_settings.py
from waldur_core.server.openapi_utils import labels_of

ENUM_NAME_OVERRIDES = {
    "OrderState": labels_of(OrderStates),  # Use labels_of() helper
}
```

## Important Guidelines

- **No Emojis**: Avoid emojis unless explicitly requested
- **Avoid "Comprehensive"**: Don't use this word in documentation
- **Incremental Changes**: Make small, testable changes
- **Existing Patterns**: Follow established project patterns
