# How to write tests

## Application tests structure

Application tests should follow next structure:

- **/tests/** - folder for all application tests.

- **/tests/test_my_entity.py** - file for API calls tests that are logically related to entity.
  Example: test calls for project CRUD + actions.

- **/tests/test_my_entity.py:MyEntityActionTest** - class for tests that are related to particular endpoint.
  Examples: ProjectCreateTest, InstanceResizeTest.

- **/tests/unittests/** - folder for unittests of particular file.

- **/tests/unittests/test_file_name.py** - file for test of classes and methods
  from application file "file_name". Examples: test_models.py, test_handlers.py.

- **/tests/unittests/test_file_name.py:MyClassOrFuncTest** - class for test that is related to particular class or
  function from file. Examples: ProjectTest, ValidateServiceTypeTest.

## Tips for writing tests

- cover important or complex functions and methods with unittests;
- write at least one test for a positive flow for each endpoint;
- do not write tests for actions that does not exist. If you don't support
  "create" action for any user there is no need to write test for that;
- use fixtures (module fixtures.py) to generate default structure.

## How to override settings in unit tests

Don't manipulate django.conf.settings directly as Django won't restore the original values after such manipulations.
Instead you should use standard [context managers and decorators](https://docs.djangoproject.com/en/2.2/topics/testing/tools/#overriding-settings).
They change a setting temporarily and revert to the original value after running the testing code.
If you modify settings directly, you break test isolation by modifying global variable.

If configuration setting is not plain text or number but dictionary, and you need to update only one parameter,
you should take whole dict, copy it, modify parameter, and override whole dict.

Wrong:

```python
  with self.settings(WALDUR_CORE={'INVITATION_LIFETIME': timedelta(weeks=1)}):
    tasks.cancel_expired_invitations()
```

Right:

```python
  waldur_settings = settings.WALDUR_CORE.copy()
  waldur_settings['INVITATION_LIFETIME'] = timedelta(weeks=1)

  with self.settings(WALDUR_CORE=waldur_settings):
    tasks.cancel_expired_invitations()
```

## Running tests

In order to run unit tests for specific module please execute the following command.
Note that you should substitute module name instead of example waldur_openstack.
Also it is assumed that you've already activated virtual Python environment.

```bash
  DJANGO_SETTINGS_MODULE=waldur_core.server.test_settings waldur test waldur_openstack
```
