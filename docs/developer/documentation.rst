Documentation policy FAQ
========================

1. Do I need to put all endpoints docs in one file or separately?

- API endpoints docs go to source code and are extracted into RST.

2. Where I should add general plugin description?

    2 places:

    - Link in Waldur docs.
    - Expanded overview in the Introduction section of the plugin docs.

3. Where should I describe plugins objects features - quotas, cost tracking, etc?

    - In the guide section.

5. Where can I see all development policies and guides?

    - Waldur documentation, developer section.


API documentation
=================

Waldur generates API documentation based on docstrings from following classes:

- **AppConfig class** docstring should give general information about an app
- **View class** docstring should describe intention of top-level endpoint
- **View method** docstring should explain usage of particular method or actions

Endpoints are grouped by `Django apps <https://docs.djangoproject.com/en/1.11/ref/applications/#module-django.apps>`_
in `RST` files located in *docs/drfapi*.

Use following command to generate `RST` files for the API:

.. code-block:: bash

    $ waldur drfdocs

Note that you should have all development requirements specified in `setup.py` file properly installed.

In order to specify docstring for list views you can override **list** method.

For example,

    .. code-block:: python

        def list(self, request, *args, **kwargs):
            """
            To get a list of instances, run **GET** against */api/openstack-instances/* as authenticated user.
            Note that a user can only see connected instances:
            """
            return super(InstanceViewSet, self).list(request, *args, **kwargs)


In order to specify docstring for detail views you can override **retrieve** method.

For example,

    .. code-block:: python

        def retrieve(self, request, *args, **kwargs):
            """
            To stop/start/restart an instance, run an authorized **POST** request against the instance UUID,
            appending the requested command.
            """

            return super(InstanceViewSet, self).retrieve(request, *args, **kwargs)
