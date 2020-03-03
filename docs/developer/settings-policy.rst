Settings policy
===============

Settings are used to configure behaviour of Waldur deployment. Settings can be used for configuration of both
core and plugins, or dependent libraries.

Below is a policy for the settings.

Base settings
-------------

TODO

Plugin settings
---------------

Plugins are defining their settings in the **extension.py**. However, most probably not all settings might make sense to
override in production. Responsibility for highlighting what settings could be overridden in production are on
plugin developer.

Deployment settings
-------------------

Deployment specific settings (e.g. for CentOS-7) are maintained as Python files and are kept in **/etc/waldur/**.
They are read in by a packaging specific **packaging/settings.py** file copied to PYTHONPATH
(**waldur_core.server.settings**) in packaging branch.

The following settings files are read in from **/etc/waldur/**:

 - **override.conf.py** - all settings overwritten for the deployment apart from logging settings.

 - **core.ini** - contains basic settings for the deployment in INI format.
