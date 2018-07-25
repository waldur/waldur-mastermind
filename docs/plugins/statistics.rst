Statistics
==========

Usage statistics
----------------

Warning! This endpoint is restricted to IAAS application.

Historical data of usage aggregated by projects/customers.

URL: **/api/stats/usage/**

Available request parameters:

- ?aggregate=aggregate_model_name (default: 'customer'. Have to be from list: 'customer', 'project')
- ?uuid=uuid_of_aggregate_model_object (not required. If this parameter will be defined - result will contain only
  object with given uuid; multiple values could be supplied)
- ?item=instance_usage_item (required. Have to be from list: 'cpu', 'memory', 'storage').
  CPU is reported as utilisation and goes from 0 to 100% as reported by 'ps -o %cpu'. Memory and storage are in MiB.
- ?from=timestamp (default: now - one hour, example: 1415910025)
- ?to=timestamp (default: now, example: 1415912625)
- ?datapoints=how many data points have to be in answer(default: 6)

Answer will be list of dictionaries with fields:

- name - name of aggregate object (customer or project)
- datapoints - list of datapoints for aggregate object.
  Each datapoint is a dictionary with fields: 'from', 'to', 'value'. Datapoints are sorted in ascending time order.


Example:

.. code-block:: javascript

    [
        {
            "name": "Proj27",
            "datapoints": [
                {"to": 471970877, "from": 1, "value": 0},
                {"to": 943941753, "from": 471970877, "value": 0},
                {"to": 1415912629, "from": 943941753, "value": 3.0}
            ]
        },
        {
            "name": "Proj28",
            "datapoints": [
                {"to": 471970877, "from": 1, "value": 0},
                {"to": 943941753, "from": 471970877, "value": 0},
                {"to": 1415912629, "from": 943941753, "value": 3.0}
            ]
        }
    ]


Customer statistics
-------------------
Warning! This endpoint is restricted to IAAS application.

Summary of projects/groups/vms per customer.

URL: **/api/stats/customer/**

No input parameters. Answer will be list dictionaries with fields:

- name - customer name
- projects - count of customers projects
- instances - count of customers instances

Example:

.. code-block:: python

    [
        {"instances": 4, "name": "Customer5", "projects": 2}
    ]


Resource statistics
-------------------
Warning! This endpoint is restricted to IAAS application.

Allocation of resources in a cloud backend.

URL: **/api/stats/resource/**

Required request GET parameter: *?auth_url* - cloud URL

Answer will be list dictionaries with fields:

**vCPUs:**

- vcpus_used - currently number of used vCPUs
- vcpu_quota - maximum number of vCPUs (from quotas)
- vcpus - maximum number of vCPUs (from hypervisors)

**Memory:**

- free_ram_mb - total available memory space on all physical hosts
- memory_mb_used - currently used memory size on all physical hosts
- memory_quota - maximum number of memory (from quotas)
- memory_mb - total size of memory for allocation

**Storage:**

- free_disk_gb - total available disk space on all physical hosts
- storage_quota - allocated storage quota


Example:

.. code-block:: javascript

    {
        "free_disk_gb": 14,
        "free_ram_mb": 510444,
        "memory_mb": 516588,
        "memory_mb_used": 6144,
        "memory_quota": 0,
        "storage_quota": 0,
        "vcpu_quota": 0,
        "vcpus": 64,
        "vcpus_used": 4
    }



Alerts statistics
-----------------

Warning! This endpoint is *deprecated* use **/api/alerts/stats/** instead of it.

Health statistics based on the alert number and severity. You may also narrow down statistics by instances aggregated
by specific projects or customers.

URL: **/api/stats/alert/**

All available request parameters are optional:

- ?from=timestamp
- ?to=timestamp
- ?aggregate=aggregate_model_name (default: 'customer'. Have to be from list: 'customer', 'project')
- ?uuid=uuid_of_aggregate_model_object (not required. If this parameter will be defined - result will contain only
  object with given uuid)
- ?opened - if this argument is in GET request - endpoint will return statistics only for alerts that are not closed
- ?alert_type=<alert_type> (can be list)
- ?scope=<url> concrete alert scope
- ?scope_type=<string> name of scope type (Ex.: instance, cloud_project_membership, project...)
- ?acknowledged=True|False - show only acknowledged (non-acknowledged) alerts
- ?created_from=<timestamp>
- ?created_to=<timestamp>
- ?closed_from=<timestamp>
- ?closed_to=<timestamp>


Answer will be dictionary where key is severity and value is a count of alerts.

Example:

.. code-block:: javascript

        {
            "Debug": 2,
            "Error": 1,
            "Info": 1,
            "Warning": 1
        }
