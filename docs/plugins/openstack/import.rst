Resources import
----------------

Waldur is able to import existing OpenStack tenants, instances and volumes.
This allows you take resources you've created and bring it under Waldur management.

To get list of available volumes to import send GET request to *openstacktenant-volumes/importable_resources* endpoint.
Service project link ID is a required parameter to fetch importable volumes from OpenStack.

To import volume send the following parameters to *openstacktenant-volumes/import* endpoint:

- **service_project_link** a service project url to associate imported volume with;
- **backend_id** a backend id of the resource to be imported.
