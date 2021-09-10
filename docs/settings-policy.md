# Settings policy

Settings are used to configure behaviour of Waldur deployment. Settings can be used for configuration of both
core and plugins, or dependent libraries.

Below is a policy for the settings.

## Plugin settings

Plugins are defining their settings in the `extension.py`. However, most probably not all settings might make sense to
override in production. Responsibility for highlighting what settings could be overridden in production are on
plugin developer.

## Deployment settings

Deployment specific settings (e.g. for CentOS-8) are maintained as Python files and are kept in `/etc/waldur/`:

- `override.conf.py` - all settings overwritten for the deployment apart from logging settings;
- `features.json` - features setup for frontend;
- `logging.conf.py` - logging configuration for Waldur
