How to write imports
====================

Grouping
--------

1. Imports from :code:`__future__`.
2. Default python modules.
3. Related third party imports.
4. Imports from installed Waldur modules.
5. Local application imports.

Example:

.. code-block:: python

    from __future__ import unicode_literals

    import datetime

    from django.conf import settings
    from model_utils import FieldTracker

    from waldur_core.core import models as core_models, exceptions as core_exceptions
    from waldur_core.structure import models as structure_models
    from waldur_openstack.openstack import models as openstack_models

    from waldur_mastermind.packages import models as package_models
    from . import utils, managers


Ordering
--------

In each group imports should be ordered in alphabetical order, regardless
import keyword.

Wrong:

.. code-block:: python

    from os import path
    import datetime

Right:

.. code-block:: python

    import datetime
    from os import path

(Because we ignore keywords 'from', 'import' and should order by package name)


Other rules
-----------

1. Use relative import when you are importing module from the same application.

In openstack plugin:

Wrong:

.. code-block:: python

    from waldur_openstack.openstack import models

Right:

.. code-block:: python

    from . import models


2. Group import from one module in one line.

Wrong:

.. code-block:: python

    from waldur_core.core import models as core_models
    from waldur_core.core import exceptions as core_exceptions

Right:

.. code-block:: python

    from waldur_core.core import models as core_models, exceptions as core_exceptions


3. It is suggested to import whole modules from waldur_core plugin, not only
separate classes.

Wrong:

.. code-block:: python

    from waldur_core.structure.models import Project

Right:

.. code-block:: python

    from waldur_core.structure import models as structure_models
