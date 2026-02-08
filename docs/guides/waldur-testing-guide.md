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

### 6. Test Base Class Selection

Choose the right test base class for each test:

- **Default: `test.APITestCase`** — uses transaction rollback, much faster
- **Use `test.APITransactionTestCase`** only when:
  1. `transaction.on_commit()` callbacks must fire (e.g., Celery task dispatch)
  2. `IntegrityError` is deliberately triggered (breaks TestCase's wrapping transaction)
  3. Threading or multi-process database access is needed
  4. `responses.start()` in `setUp` for class-wide HTTP mocking (leaks across TestCase classes)

```python
# GOOD: Default to APITestCase
class MyTest(test.APITestCase):
    def test_something(self):
        ...

# GOOD: Use APITransactionTestCase when on_commit is needed
class OrderProcessingTest(test.APITransactionTestCase):
    def test_order_triggers_task(self):
        # on_commit callback fires Celery task
        ...
```

A CI lint job (`scripts/analyze_transaction_test_cases.py --ci --baseline N`) enforces this — adding new unjustified `APITransactionTestCase` classes will fail the pipeline. The baseline is lowered as classes are migrated.

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

### 10. HTTP Mocking Patterns

**Preferred: `@responses.activate` per method** — fully isolated, no cleanup needed:

```python
class MyTest(test.APITestCase):
    @responses.activate
    def test_external_call(self):
        responses.add(responses.GET, "https://api.example.com/data", json={"ok": True})
        result = my_function()
        self.assertEqual(result, {"ok": True})
```

**Class-wide mocking with `responses.start()`** — requires `APITransactionTestCase`:

```python
class ExternalAPITest(test.APITransactionTestCase):
    """responses.start() in setUp leaks state across TestCase classes."""

    def setUp(self):
        super().setUp()
        responses.start()
        responses.add(responses.GET, "https://api.example.com/data", json={"ok": True})

    def tearDown(self):
        responses.stop()
        responses.reset()
        super().tearDown()
```

Using `responses.start()` in `setUp` with `APITestCase` causes leaked mock state across test classes because `TestCase` doesn't fully reset process-level state between classes.

### 11. Multiple Inheritance Pitfall

When combining `APITransactionTestCase` with a mixin that extends `APITestCase`, Python's MRO can silently break `TransactionTestCase` behavior:

```python
# BAD: MRO puts TestCase._fixture_teardown first
class MyTest(test.APITransactionTestCase, SomeTestMixin):
    ...  # SomeTestMixin extends APITestCase — TransactionTestCase teardown is skipped

# GOOD: Ensure all parents use TransactionTestCase, or use standalone setup
class MyTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        # Set up mocks directly instead of inheriting from a TestCase mixin
```

### 12. OpenStack Backend Test Patterns

When writing standalone backend tests that don't inherit from `BaseBackendTestCase`:

```python
class StandaloneBackendTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = openstack_fixtures.OpenStackFixture()
        # Mock all 5 OpenStack clients
        self.mock_admin = mock.patch("waldur_openstack.openstack_base.backend.AdminSession").start()
        self.mock_session = mock.patch("waldur_openstack.openstack_base.backend.SessionManager").start()
        self.mock_nova = mock.patch("waldur_openstack.openstack_base.backend.NovaClient").start()
        self.mock_neutron = mock.patch("waldur_openstack.openstack_base.backend.NeutronClient").start()
        self.mock_cinder = mock.patch("waldur_openstack.openstack_base.backend.CinderClient").start()

    def tearDown(self):
        mock.patch.stopall()
        super().tearDown()
```

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
