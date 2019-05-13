Installation from source
------------------------

Additional requirements:

- ``git``
- ``redis`` and ``hiredis`` library
- ``virtualenv``
- C compiler and development libraries needed to build dependencies

  - CentOS: ``gcc libffi-devel openssl-devel postgresql-devel libjpeg-devel zlib-devel python-devel xmlsec1``
  - Ubuntu: ``gcc libffi-dev libsasl2-dev libssl-dev libpq-dev libjpeg8-dev zlib1g-dev python-dev xmlsec1``

**Waldur MasterMind installation**

1. Get the code:

  .. code-block:: bash

    git clone https://github.com/opennode/waldur-mastermind.git

2. Create a Virtualenv and update Setuptools:

  .. code-block:: bash

    cd waldur-mastermind
    virtualenv venv
    venv/bin/pip install --upgrade setuptools

3. Install Waldur in development mode along with dependencies:

  .. code-block:: bash

    venv/bin/pip install --requirement docker-test/api/requirements.txt
    venv/bin/pip install --editable .

4. Create and edit settings file (see 'Configuration' section for details):

  .. code-block:: bash

    cp src/waldur_core/server/settings.py.example src/waldur_core/server/settings.py
    vi src/waldur_core/server/settings.py

5. Initialise PostgreSQL database:

  .. code-block:: bash

    createdb waldur
    createuser waldur
    venv/bin/waldur migrate --noinput

6. Collect static data -- static files will be copied to ``./static/`` in the same directory:

  .. code-block:: bash

    venv/bin/waldur collectstatic --noinput

7. Start Waldur:

  .. code-block:: bash

    venv/bin/waldur runserver

Configuration
+++++++++++++

Instructions are here: http://docs.waldur.com/MasterMind+configuration

Caveats
+++++++

By default Python package is installed in standalone mode.
It means that its contents is copied to the ``site-packages`` directory.
But for development you should install package as editable by passing
``--editable`` or simply ``-e`` flag to the ``pip`` command.
In this case package contents is not copied, only symbolic link is created.
You may accidentally install the same package as standalone and editable simultaneously.
In this case the following exception would be raised: ``ImportError: No module named settings``
In order to fix it you should uninstall package and install it again as editable.
