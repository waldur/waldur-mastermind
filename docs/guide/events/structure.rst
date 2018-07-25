Structure events
++++++++++++++++

.. glossary::

    **customer_creation_succeeded**
        Customer has been created.

    **customer_update_succeeded**
        Customer has been updated.

    **customer_deletion_succeeded**
        Customer has been deleted.

------------

.. glossary::

    **user_organization_claimed**
        User has claimed organization.

    **user_organization_approved**
        User has been approved for organization.

    **user_organization_rejected**
        User claim for organization has been rejected.

    **user_organization_removed**
        User has been removed from organization.

------------

.. glossary::

    **project_creation_succeeded**
        Project has been created.

    **project_name_update_succeeded**
        Project name has been updated.

    **project_update_succeeded**
        Project has been updated.

    **project_deletion_succeeded**
        Project has been deleted.

------------

.. glossary::

    **role_granted**
        User has gained role.

    **role_revoked**
        User has lost role.

------------

Resource events are generic and contain a field **resource_type** that can be used for discriminating what has been
affected. Possible values depend on the plugins enabled, for example OpenStack.Instance or SaltStack.ExchangeTenant.


.. glossary::

   **resource_creation_scheduled**
   **resource_creation_succeeded**
   **resource_creation_failed**

      Resource creation events. Emitted on creation of all events, i.e. both VMs and applications.

   **resource_update_succeeded**

      Resource update has been updated.

   **resource_deletion_scheduled**
   **resource_deletion_succeeded**
   **resource_deletion_failed**

      Resource deletion events.

   **resource_start_scheduled**
   **resource_start_succeeded**
   **resource_start_failed**
   **resource_stop_scheduled**
   **resource_stop_succeeded**
   **resource_stop_failed**
   **resource_restart_scheduled**
   **resource_restart_succeeded**
   **resource_restart_failed**

      Events for resources that can change state from online to offline, i.e. virtual machines.

   **resource_import_succeeded**

      Resource has been imported.
