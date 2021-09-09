# How to write docs

## Documentation for sysadmins

Documentation for sysadmins should contain a description of settings
that allows to setup and customize Waldur MasterMind. It should be
located in [wiki](https://docs.waldur.com/admin-guide/mastermind-configuration/configuration-guide/).

## Documentation for developers

If documentation describes basic concepts that are not related to any
particular part of code it should be located in `/docs` folder. All other documentation for developers should be located in code.

Tips for writing docs:

- add description for custom modules that are unique for particular plugin;
- add description to base class methods that should be implemented by other developers;
- don't add obvious comments for standard objects or parameters.
