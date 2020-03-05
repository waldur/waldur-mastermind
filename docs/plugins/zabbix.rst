Zabbix plugin
-------------

Waldur can be used for implementing a MaaS solution for OpenStack VMs with Zabbix monitoring service.

Two approaches for MaaS are available:

1. A pre-packaged Zabbix appliance deployed into defined OpenStack tenant for
the highest flexibility.

2. A pre-packaged Zabbix appliance configurable by Waldur after the
deployment of the appliance ("Advanced monitoring"). Intended for use cases
when OpenStack hosts need to be registered manually or automatically in the
monitoring server deployed in a tenant.

Below we describe configuration approach for both of the cases.

Zabbix appliance only
+++++++++++++++++++++

Setup
*****

1. Create a template group:

  - name, description, icon_url - support parameters for the application store
  - tags - PaaS

2. Add OpenStack Instance template to the template group with the following settings:

  - tags - PaaS, license-application:zabbix:Zabbix-3.0, license-os:centos7:CentOS-7-x86_64, support:premium
  - service settings - OpenStack settings where a VM needs to be provisioned
  - flavor - default configuration for the created Zabbix server
  - image - OpenStack image with pre-installed Zabbbix
  - data volume, system volume - default size for Zabbix deployments


Supported operations by REST client
***********************************

Zabbix appliance is a basic OpenStack image that supports the following provisioning
inputs:

 - name
 - project
 - security groups
 - user_data

User data can be used to setup Zabbix admin user password:

.. code-block:: yaml

    #cloud-config
    runcmd:
      - [ bootstrap, -a, <Zabbix admin user password> ]


Advanced monitoring
+++++++++++++++++++

Provisioning flow
*****************

Waldur requires a separate template group for advanced monitoring that
contains 2 templates:

- OpenStack VM template - describing provision details of a new VM with Zabbix;

- Zabbix service template - creating a Zabbix service, based on created VM details.


Setup
*****

1. Add settings for SMS sending to Waldur settings:

.. code-block:: python


    WALDUR_ZABBIX = {
        'SMS_SETTINGS': {
            'SMS_EMAIL_FROM': 'zabbix@example.com',
            'SMS_EMAIL_RCPT': '{phone}@example.com',
        },
    }


2. Add Zabbix security group to all existing tenants:

.. code-block:: bash

  waldur initsecuritygroups zabbix
  waldur initsecuritygroups zabbix-agent

3. Create template group:

  - name, description, icon_url - support parameters for the application store
  - tags - SaaS

4. Add OpenStack instance provision template:

  - tags - SaaS, license-application:zabbix:Zabbix-3.0, license-os:centos7:CentOS-7-x86_64, support:advanced
  - service settings - OpenStack settings where a VM needs to be provisioned
  - flavor - choose suitable for Zabbix image
  - image - OpenStack image with pre-installed Zabbbix
  - data volume, system volume - default size for Zabbix deployments
  - user data:

.. code-block:: yaml

  #cloud-config
  runcmd:
    - [ bootstrap, -a, {{ 8|random_password }}, -p, {{ 8|random_password }}, -l, "%", -u, waldur ]


  {{ 8|random_password }} will generate a random password with a length of 8

5. Add Zabbix service provision template:

  - order_number - 2 (should be provisioned after OpenStack VM)
  - name - {{ response.name }} (use VM name for service)
  - scope - {{ response.url }} (tell service that it is located on given VM)
  - use project of the previous object - True (connect service to VM project)
  - backend url - http://{{ response.access_url.0 }}/zabbix/api_jsonrpc.php (or https)
  - username - Admin
  - password - {{ response.user_data|bootstrap_opts:"a" }}
  - tags - advanced
  - database parameters:

.. code-block:: json

   {
        "engine": "django.db.backends.mysql",
        "name": "zabbix",
        "host": "XXX",
        "user": "waldur",
        "password": "{{ response.user_data|bootstrap_opts:'p' }}",
        "port": "3306"
   }

Parameter "host" should be specified based on environment and Zabbix image
configuration.


Requests from frontend
**********************

1. To create instance with advance monitoring issue POST request to template_group provision endpoint with project, name
   and security group named "zabbix".

2. To get list of all available for instance advanced zabbix services - issue GET request against **/api/zabbix/** with
   parameters:

    - project=<instance project>
    - tag=advanced

3. To create host for instance - issue POST request against **/api/zabbix-hosts/** with instance url as scope. Check
   endpoint details for other parameters details.

4. Instance advanced monitoring can be enabled/disabled by changing host status with PUT/PATCH request against
   **/api/zabbix-hosts/<uuid>/**.

5. If instance is already monitored - host will appear in <related_resources> with tag "advanced" in service_tags field.

6. Instance advanced monitoring can be configured with PUT/PATCH request against **/api/zabbix-hosts/<uuid>/**.
