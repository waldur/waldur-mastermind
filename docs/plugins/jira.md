# JIRA plugin

## Configuration

1.  Define active backend.

> ``` python
> # For Service Desk
> WALDUR_SUPPORT.update({
>     'ACTIVE_BACKEND': 'waldur_mastermind.support.backend.atlassian:ServiceDeskBackend',
> })
> # For JIRA
> WALDUR_SUPPORT.update({
>     'ACTIVE_BACKEND': 'waldur_mastermind.support.backend.atlassian:JiraBackend',
> })
> ```

2.  Setup connection. Define server URL and user details to connect JIRA
    or Service Desk to Waldur:

> ``` python
> WALDUR_SUPPORT['CREDENTIALS'].update({
>     'server': <server URL>,
>     'username': <Atlassian user username>,
>     'password': <Atlassian user password>,
> })
> ```

3.  Project setup. Define project key.

> ``` python
> WALDUR_SUPPORT['PROJECT'].update({
>     'key': <project key>,
> })
> ```

4.  Project issues setup.

    4.1. Make sure that selected project supports registered types of issues: `WALDUR_SUPPORT['ISSUE']['types']`.

    4.2. Make sure that project issues have fields that corresponds to
    `impact_field`, `reporter_field`, `caller_field`. It is
    possible to override default field names:

    > ``` python
    > WALDUR_SUPPORT['ISSUE'].update({
    >     'impact_field': <issue impact field name in JIRA or Service desk>,
    >     'reporter_field': <issue reporter field name in JIRA or Service desk>,
    >     'caller_field': <issue caller field name in JIRA or Service desk>,
    > })
    > ```

## Web hook installation

It's possible to track updates of JIRA issues and apply them to Waldur
immediately.

An instruction of JIRA configuration can be found at
<https://developer.atlassian.com/jiradev/jira-apis/webhooks>

Step by step guide:

1.  Log in to JIRA as administrator

2.  Click on a cogwheel in the upper right corner and pick 'System'.

3.  Scroll down to the lower left corner and find a "WebHook" option under the Advanced tab.

4. Now click on "Create a Web Hook" You will be presented with a web
hook creation view. There are only 3 mandatory fields - Name, Status and URL.

    4.1 Name your hook

    4.2 Select whether you want to enable it. It can be disabled at any
    moment from to the same menu.

    4.3 Configure a URL to send a POST request to. For instance:
    <http://waldur.example.com/api/support-jira-webhook/> It is not needed
    to add any additional fields to request.

    *Note: In case of VirtualBox localhost usually is 10.0.2.2. So the
    complete URL will be next:
    http://10.0.2.2:8000/api/support-jira-webhook/*

    4.4 Add a description.

    4.5 Please make sure you've picked 'created, updated and deleted' actions under 'Events' section. No need to to check Comments events, they will be synced by the issue triggers.

    4.6 Save configuration.
