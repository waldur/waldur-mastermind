
JIRA Installation Guide
-----------------------

1. Download JIRA Install
www.atlassian.com/software/jira/download

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


WebHook Setup
-------------

It's possible to track updates of JIRA issues and apply them to NodeConductor immediately.

An instruction of JIRA configuration can be found at
https://developer.atlassian.com/jiradev/jira-apis/webhooks

Step by step guide:
^^^^^^^^^^^^^^^^^^^

1. Log in to JIRA as administrator


2. Click on a cogwheel in the upper right corner and pick 'System'.

3. Scroll down to the lower left corner and find a "WebHook" option under the Advanced tab.

4. Now click on "Create a Web Hook"
You will be presented with a web hook creation view. There are only 3 mandatory fields - Name, Status and Url.

4.1 Name your hook

4.2 Select whether you want to enable it. It can be disabled at any moment from to the same menu.

4.3 Configure a url to send a "POST" request to. For instance: http://nodeconductor.example.com/api/support-jira-webhook/
It is not needed to add any additional fields to request.

4.4 Add a description.

4.5 Please make sure you've picked 'created, updated and deleted' actions under 'Events' section.
No need to to check Comments events, they will be synced by the issue triggers.

4.6 Save configuration.
