# Resources import

Waldur is able to import existing OpenStack tenants, instances and
volumes. This allows you take resources you've created and bring it
under Waldur management.

To get list of available volumes to import send GET request to
*marketplace-offerings/<offering_uuid>/importable_resources/* endpoint.

To import volume send POST-request with the following parameters to
*marketplace-offerings/<offering_uuid>/import_resource* endpoint:

- **project** a UUID of project used for import;
- **backend_id** a backend id of the resource to be imported;
- **plan** an optional UUID of plan used for import.
