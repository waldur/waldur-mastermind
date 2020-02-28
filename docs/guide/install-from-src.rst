Installation from source
------------------------

Additional requirements:

- ``git``
- ``redis`` and ``hiredis`` library
- ``virtualenv``
- C compiler and development libraries needed to build dependencies

  - CentOS: ``gcc libffi-devel openssl-devel postgresql-devel libjpeg-devel zlib-devel python-devel xmlsec1 xz-devel``
  - Ubuntu: ``gcc libffi-dev libsasl2-dev libssl-dev libpq-dev libjpeg8-dev zlib1g-dev python-dev xmlsec1 libldap2-dev liblzma-dev``

**Waldur MasterMind installation**

1. Install poetry:

  .. code-block:: bash

    pip3 install poetry

2. Get the code:

  .. code-block:: bash

    git clone https://github.com/opennode/waldur-mastermind.git
    cd waldur-mastermind

3. Install Waldur in development mode along with dependencies:

  .. code-block:: bash

    poetry install

4. Create and edit settings file (see 'Configuration' section for details):

  .. code-block:: bash

    cp src/waldur_core/server/settings.py.example src/waldur_core/server/settings.py
    vi src/waldur_core/server/settings.py

5. Initialise PostgreSQL database:

  .. code-block:: bash

    createdb waldur
    createuser waldur
    poetry run waldur migrate --noinput

6. Collect static data -- static files will be copied to ``./static/`` in the same directory:

  .. code-block:: bash

    poetry run waldur collectstatic --noinput

7. Start Waldur:

  .. code-block:: bash

    poetry run waldur runserver

Configuration
+++++++++++++

Instructions are here: http://docs.waldur.com/MasterMind+configuration
