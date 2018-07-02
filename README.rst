This repository contains collection of Ansible modules to allow provisioning and
management of infrastructure under Waldur through Ansible playbooks.

Supported functionality
=======================
- OpenStack virtual machine provisioning.
- OpenStack security group provisioning.
- OpenStack floating IP assignment.
- OpenStack volume provisioning.
- OpenStack snapshot provisioning.

See also: http://docs.ansible.com/ansible/modules.html


Example usage
=============

Configure an Ansible playbook with parameters
---------------------------------------------
.. code-block:: yaml

  name: Trigger master instance
  waldur_os_instance:
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

    ANSIBLE_LIBRARY=/usr/share/ansible-waldur/ ansible \
        -m waldur_os_get_instance \
        -a "api_url=https://waldur.example.com/api/ access_token=9036194e1ac54cada3248a8c6b203bf7 name=instance-name project='Project name'" \
        localhost


Running playbook using virtual Python environment
-------------------------------------------------
If you've installed Ansible Waldur module to virtual Python environment you need to specify
path to Python interpreter and path to module library along with path to playbook:

.. code-block:: bash

    ansible-playbook \
        -e ansible_python_interpreter=/home/user/ansible-env/bin/python \
        -M /home/user/ansible-env/lib/python3.6/site-packages/ \
        playbook.yml


Contributing
============

See also: https://docs.ansible.com/ansible/latest/dev_guide/developing_modules_general.html
