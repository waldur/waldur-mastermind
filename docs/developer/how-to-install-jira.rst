How to install JIRA
-------------------

1. Download JIRA Install www.atlassian.com/software/jira/download

1.1 Please make sure the system for installation is prepared and you have selected the right version of JIRA.

For Windows:
^^^^^^^^^^^^

2.1. Run an installer and follow the installation wizard

For Linux:
^^^^^^^^^^

2.1. Place downloaded image on the server

2.2. Make the package executable:

.. code-block:: sh

    chmod a+x atlassian-jira-software-X.X.X-x64.bin


2.3. Run the installer:

.. code-block:: sh

    sudo ./atlassian-jira-software-X.X.X-x64.bin

Post installation steps
^^^^^^^^^^^^^^^^^^^^^^^

3. After JIRA is installed open the port displayed after installation is finished. Usually it is 8080.

4. Configure JIRA by following an installation guide in your favourite browser.

PS. If you are installing JIRA on a virtual machine please make sure that port forwarding is configured.
