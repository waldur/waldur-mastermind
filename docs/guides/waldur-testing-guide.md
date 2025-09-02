# Waldur Testing Guide

## Test Writing Best Practices

### 1. Understand Actual System Behavior

- **Always verify actual behavior before writing tests** - Don't assume how the system should work
- **Test what the system actually does, not what you think it should do**
- Example: Basic permission queries don't automatically filter expired roles

### 2. Use Existing Fixtures and Factories

- **Always use established fixtures** - Don't invent your own role names
- Use `CustomerRole.SUPPORT` not `CustomerRole.MANAGER` (which doesn't exist)
- Use `fixtures.ProjectFixture()` for consistent test setup with proper relationships
- Use `factories.UserFactory()` for creating test users with proper defaults

### 3. Error Handling Reality Check

- **Test for actual exceptions, not ideal ones**
- If the system raises `AttributeError` for missing attributes, test for `AttributeError`
- Only test for `PermissionDenied` when the system actually catches and converts errors

### 4. Mock Objects for Complex Testing

- **Use Mock objects effectively for nested permission paths**
- Create realistic mock structures: `mock_resource.project.customer = self.customer`
- Test permission factory with multiple source paths: `["direct_customer", "project.customer"]`
- Mock objects help test complex scenarios without database overhead

### 5. Time-Based Testing Patterns

- **Understand explicit vs implicit time checking**
- Basic `has_permission()` doesn't check expiration times automatically
- Test boundary conditions: exact expiration time, microseconds past expiration
- Create roles with `timezone.now() ± timedelta()` for realistic time testing

### 6. Integration vs Unit Test Strategy

- **Use integration tests for workflows**, unit tests for utilities
- Test complete permission flows: role creation → permission assignment → permission checking
- Use `APITransactionTestCase` for integration tests requiring database transactions
- Use `TestCase` for simple unit tests of utility functions

### 7. Performance Testing Considerations

- **Include query optimization tests** where appropriate
- Use `override_settings(DEBUG=True)` to count database queries
- Test with multiple users/roles to ensure performance doesn't degrade

### 8. System Role Protection

- **Test that system roles work correctly** even when modified
- System roles like `CustomerRole.OWNER` should maintain functionality
- Test that role modifications don't break core functionality
- Verify that predefined roles have expected permissions

### 9. Edge Case Testing

- **Test None values, missing attributes, and circular references**
- Handle `AttributeError` when accessing missing nested attributes
- Test with inactive users, deleted roles, removed permissions
- Verify behavior with complex nested object hierarchies

## Test Guidelines

- Test behavior, not implementation
- One assertion per test when possible
- Clear test names describing scenario
- Use existing test utilities/helpers
- Tests should be deterministic

## Debugging Complex Systems

When fixing performance or accuracy issues:

1. **Isolate the problem**:
  - Run individual failing tests to understand specific issues
  - Use `pytest -v -s` for verbose output with print statements
  - Check if multiple tests fail for the same underlying reason

2. **Understand test expectations**:
  - Read test comments carefully - they often explain intended behavior
  - Check if tests expect specific error types
  - Look for conflicting expectations between test suites

3. **Fix systematically**:
  - Fix one root cause at a time
  - After each fix, run full test suite to check for regressions
  - Update related tests for consistency when changing behavior

4. **API changes require test updates**:
  - When changing function signatures or default parameters, expect test failures
  - Update tests for consistency rather than reverting functional improvements
  - Document parameter behavior changes clearly
