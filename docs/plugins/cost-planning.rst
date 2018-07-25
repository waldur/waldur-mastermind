Cost planning plugin
--------------------

This plugin allows to get a price estimate without actually creating the infrastructure, for example:

- admin creates categories: webservers and databases;
- admin creates presets, Apache and MySQL, each preset is linked to set of default price list items;
- user creates new deployment plan for his customer;
- user selects several presets, for example 20 MySQL databases and 2 Apache servers;
- user selects service, for example Azure;
- price list items are found by matching default price list items of presets against selected service;
- total price for deployment plan is calculated;
- user generates and downloads PDF report with deployment plan details;
- user sends email with deployment plan details to another user.
