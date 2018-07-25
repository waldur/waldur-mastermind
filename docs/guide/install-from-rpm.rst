Installation from RPM repository
--------------------------------

To install Waldur standalone on RHEL7-compatible operating systems (CentOS 7, Scientific Linux 7):

.. literalinclude:: bootstrap-centos7.sh
   :language: bash

All done. Waldur API should be available at http://myserver/api/ (port 80).

Note that database server (PostgreSQL) and key-value store (Redis) may run on a separate servers -- in this case modify installation process accordingly.

Configuration
+++++++++++++

Waldur configuration file can be found at ``/etc/waldur/core.ini``.
