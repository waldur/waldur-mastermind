This repository contains collection of Ansible modules to allow provisioning and
management of infrastructure under Waldur through Ansible playbooks.

Supported functionality
=======================
- OpenStack VM provisioning.
- Security group manipulations.
- Floating IP assignment.

See also: http://docs.ansible.com/ansible/modules.html


How to
======


Configure an Ansible playbook with parameters
---------------------------------------------
.. code-block:: yaml

  name: Trigger master instance
  waldur_os_add_instance:
    access_token: "{{ access_token }}"
    api_url: "{{ api_url }}"
    flavor: m1.micro
    floating_ip: auto
    image: CentOS 7
    name: "{{ instance_name }}"
    project: "OpenStack Project"
    provider: VPC
    ssh_key: ssh1.pub
    subnet: vpc-1-tm-sub-net-2
    system_volume_size: 40
    wait: false

Pass parameters to an Ansible playbook
--------------------------------------
.. code-block:: bash

    ANSIBLE_LIBRARY=/usr/share/ansible-waldur/ ansible -m waldur_os_get_instance -a "api_url=https://waldur.example.com/api/ access_token=9036194e1ac54cada3248a8c6b203bf7 name=instance-name project='Project name'" localhost
