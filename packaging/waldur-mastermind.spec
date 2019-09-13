%define __conf_dir %{_sysconfdir}/waldur
%define __conf_file %{__conf_dir}/core.ini
%define __data_dir %{_datadir}/waldur
%define __log_dir %{_localstatedir}/log/waldur
%define __user waldur
%define __work_dir %{_sharedstatedir}/waldur

%define __celery_conf_file %{__conf_dir}/celery.conf
%define __celery_service_name waldur-celery
%define __celery_systemd_unit_file %{_unitdir}/%{__celery_service_name}.service
%define __celerybeat_service_name waldur-celerybeat
%define __celerybeat_systemd_unit_file %{_unitdir}/%{__celerybeat_service_name}.service

%define __logrotate_dir %{_sysconfdir}/logrotate.d
%define __logrotate_conf_file %{__logrotate_dir}/waldur

%define __uwsgi_service_name waldur-uwsgi
%define __uwsgi_conf_file %{__conf_dir}/uwsgi.ini
%define __uwsgi_systemd_unit_file %{_unitdir}/%{__uwsgi_service_name}.service

%define __saml2_conf_dir %{__conf_dir}/saml2
%define __saml2_conf_file %{__conf_dir}/saml2.conf.py.example
%define __saml2_cert_file %{__saml2_conf_dir}/sp.crt
%define __saml2_key_file %{__saml2_conf_dir}/sp.pem

Name: waldur-mastermind
Summary: Waldur MasterMind
Group: Development/Libraries
Version: 3.9.7
Release: 1.el7
License: MIT
Url: https://waldur.com
Source0: %{name}-%{version}.tar.gz

# mailcap is required for /etc/mime.types of static files served by uwsgi
# openssl package is needed to generate SAML2 keys during plugin install
# python-cryptography is needed for Azure plugin
# python-django-cors-headers is packaging-specific dependency; it is not required in upstream code
# python-jira is needed for JIRA plugin
# python-libcloud is needed for AWS plugin
# python-lxml is needed for Valimo auth to work
# xmlsec1-openssl package is needed for SAML2 features to work

Requires: ansible-waldur-module >= 0.8.2
Requires: logrotate
Requires: mailcap
Requires: openssl
Requires: python-azure-sdk >= 4.0.0
Requires: python-ceilometerclient >= 2.9.0
Requires: python2-celery >= 4.2.0
Requires: python-cinderclient >= 3.1.0
Requires: python-country >= 1.20, python-country < 2.0
Requires: python-croniter >= 0.3.4, python-croniter < 0.3.6
Requires: python2-cryptography >= 1.7.2
Requires: python-digitalocean >= 1.5
Requires: python2-django >= 1.11.23, python2-django < 2.0.0
Requires: python-django-admin-tools = 0.8.0
Requires: python-django-auth-ldap >= 1.3.0
Requires: python-django-cors-headers = 2.1.0
Requires: python-django-defender >= 0.5.3
Requires: python-django-filter = 1.0.2
Requires: python-django-fluent-dashboard = 0.6.1
Requires: python-django-fsm = 2.3.0
Requires: python-django-jsoneditor >= 0.0.7
Requires: python-django-model-utils = 3.0.0
Requires: python-django-openid-auth >= 0.14-2
Requires: python-django-redis-cache >= 1.6.5
Requires: python-django-rest-framework >= 3.6.3, python-django-rest-framework < 3.7.0
Requires: python-django-rest-swagger = 2.1.2
Requires: python-django-reversion = 2.0.8
Requires: python-django-saml2 = 0.17.1-2
Requires: python-django-taggit >= 0.20.2
Requires: python-freeipa >= 0.2.2
Requires: python-glanceclient >= 1:2.8.0
Requires: python-hiredis >= 0.2.0
Requires: python-influxdb >= 4.1.0
Requires: python-iptools >= 0.6.1
Requires: python-jira >= 1.0.15-2
Requires: python-jwt >= 1.5.3
Requires: python-keystoneclient >= 1:3.13.0
Requires: python-libcloud >= 1.1.0, python-libcloud < 2.3.0
Requires: python-lxml >= 3.2.0
Requires: python-neutronclient >= 6.5.0
Requires: python-novaclient >= 1:9.1.0
Requires: python-passlib >= 1.7.0
Requires: python-paypal-rest-sdk >= 1.10.0, python-paypal-rest-sdk < 2.0
Requires: python-pillow >= 2.0.0
Requires: python-prettytable >= 0.7.1, python-prettytable < 0.8
Requires: python-psycopg2 >= 2.5.4
Requires: python-redis = 2.10.6
Requires: python-requests >= 2.14.2
Requires: python-sqlparse >= 0.1.11
Requires: python-tlslite = 0.4.8
Requires: python-urllib3 >= 1.10.1
Requires: python-vat >= 1.3.1, python-vat < 2.0
Requires: python-zabbix >= 0.7.2
Requires: python2-defusedxml >= 0.4.1
Requires: python2-pdfkit >= 0.6.1
Requires: python2-pyvmomi >= 6.7.1
Requires: PyYAML
Requires: uwsgi-plugin-python2
Requires: xmlsec1-openssl

BuildArch: noarch
BuildRoot: %{_tmppath}/%{name}-%{version}-%{release}-buildroot

# gettext package is needed to run 'django-admin compilemessages'
# python-django* packages are needed to generate static files
# python-setuptools package is needed to run 'python setup.py <cmd>'
# systemd package provides _unitdir RPM macro
BuildRequires: gettext
BuildRequires: python2-django >= 1.11.23
BuildRequires: python-django-filter = 1.0.2
BuildRequires: python-django-fluent-dashboard
BuildRequires: python-django-jsoneditor >= 0.0.7
BuildRequires: python-django-rest-framework >= 3.6.3, python-django-rest-framework < 3.7.0
BuildRequires: python-django-rest-swagger = 2.1.2
BuildRequires: python-setuptools
BuildRequires: systemd

%description
Waldur MasterMind is part of the Waldur suite - https://waldur.com - for
managing of hybrid cloud resources. It is used to control both internal
enterprise IT resources and for selling cloud services to the public.

%prep
%setup -q -n %{name}-%{version}

%build
cp packaging/settings.py src/waldur_core/server/settings.py
django-admin compilemessages

%{__python} setup.py build

%install
rm -rf %{buildroot}
%{__python} setup.py install -O1 --root=%{buildroot}

mkdir -p %{buildroot}%{_unitdir}
cp packaging%{__celery_systemd_unit_file} %{buildroot}%{__celery_systemd_unit_file}
cp packaging%{__celerybeat_systemd_unit_file} %{buildroot}%{__celerybeat_systemd_unit_file}
cp packaging%{__uwsgi_systemd_unit_file} %{buildroot}%{__uwsgi_systemd_unit_file}

mkdir -p %{buildroot}%{__conf_dir}
cp packaging%{__celery_conf_file} %{buildroot}%{__celery_conf_file}
cp packaging%{__conf_file} %{buildroot}%{__conf_file}
cp packaging%{__uwsgi_conf_file} %{buildroot}%{__uwsgi_conf_file}

mkdir -p %{buildroot}%{__data_dir}/static
cat > tmp_settings.py << EOF
# Minimal settings required for 'collectstatic' command
INSTALLED_APPS = (
    'admin_tools',
    'admin_tools.dashboard',
    'admin_tools.menu',
    'admin_tools.theming',
    'fluent_dashboard',  # should go before 'django.contrib.admin'
    'django.contrib.contenttypes',
    'django.contrib.admin',
    'django.contrib.staticfiles',
    'jsoneditor',
    'waldur_core.landing',
    'rest_framework',
    'rest_framework_swagger',
    'django_filters',
)
SECRET_KEY = 'tmp'
STATIC_ROOT = '%{buildroot}%{__data_dir}/static'
STATIC_URL = '/static/'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': ['waldur_core/templates'],
        'OPTIONS': {
            'context_processors': (
                'django.template.context_processors.debug',
                'django.template.context_processors.request',  # required by django-admin-tools >= 0.7.0
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'django.template.context_processors.i18n',
                'django.template.context_processors.media',
                'django.template.context_processors.static',
                'django.template.context_processors.tz',
            ),
            'loaders': (
                'django.template.loaders.filesystem.Loader',
                'django.template.loaders.app_directories.Loader',
                'admin_tools.template_loaders.Loader',  # required by django-admin-tools >= 0.7.0
            ),
        },
    },
]
EOF
PYTHONPATH="${PYTHONPATH}:src" django-admin collectstatic --noinput --settings=tmp_settings

mkdir -p %{buildroot}%{__log_dir}

mkdir -p %{buildroot}%{__logrotate_dir}
cp packaging%{__logrotate_conf_file} %{buildroot}%{__logrotate_conf_file}

mkdir -p %{buildroot}%{__work_dir}/media

install -d %{buildroot}%{_bindir}
install packaging/usr/bin/%{name}-check %{buildroot}%{_bindir}

install -d %{buildroot}%{__saml2_conf_dir}/
cp -r packaging/etc/waldur/saml2/attribute-maps %{buildroot}%{__saml2_conf_dir}/
install packaging%{__saml2_conf_file} %{buildroot}%{__saml2_conf_file}

%clean
rm -rf %{buildroot}

%files
%defattr(-,root,root,-)
%{python_sitelib}/*
%{_bindir}/*
%{_unitdir}/*
%{__data_dir}
%{__logrotate_dir}/*
%{__saml2_conf_dir}/attribute-maps/*
%attr(0750,%{__user},%{__user}) %{__log_dir}
%attr(0750,%{__user},%{__user}) %{__work_dir}
%config(noreplace) %{__celery_conf_file}
%config(noreplace) %{__conf_file}
%config(noreplace) %{__uwsgi_conf_file}
%config(noreplace) %{__saml2_conf_file}

%pre
# User must exist in the system before package installation, otherwise setting file permissions will fail
if ! id %{__user} 2> /dev/null > /dev/null; then
    echo "[%{name}] Adding new system user %{__user}..."
    useradd --home %{__work_dir} --shell /bin/sh --system --user-group %{__user}
fi

%post
%systemd_post %{__celery_service_name}.service
%systemd_post %{__celerybeat_service_name}.service
%systemd_post %{__uwsgi_service_name}.service

if [ "$1" = 1 ]; then
    # This package is being installed for the first time
    echo "[%{name}] Generating secret key..."
    sed -i "s,{{ secret_key }},$(head -c32 /dev/urandom | base64)," %{__conf_file}
fi

cat <<EOF
------------------------------------------------------------------------
Waldur Core installed successfully.

Next steps:

1. Configure database server connection in %{__conf_file}.
   Database server (PostgreSQL) must be running already.

2. Configure task queue backend connection in %{__conf_file}.
   Key-value store (Redis) must be running already.

3. Review and modify other settings in %{__conf_file}.

4. Create database (if not yet done):

     CREATE DATABASE waldur ENCODING 'UTF8';
     CREATE USER waldur WITH PASSWORD 'waldur';

5. Migrate the database:

     su - %{__user} -c "waldur migrate --noinput"

   Note: you will need to run this again on next Waldur Core update.

6. Start Waldur Core services:

     systemctl start %{__celery_service_name}
     systemctl start %{__celerybeat_service_name}
     systemctl start %{__uwsgi_service_name}

7. Create first staff user (if needed and not yet done):

     su - %{__user} -c "waldur createstaffuser -u staff -p staffSecretPasswordChangeMe"

All done.
------------------------------------------------------------------------
EOF

if [ "$1" = 1 ]; then
    # This package is being installed for the first time
    echo "[%{name}] Generating SAML2 keypair..."
    if [ ! -f %{__saml2_cert_file} -a ! -f %{__saml2_key_file} ]; then
        openssl req -batch -newkey rsa:2048 -new -x509 -days 3652 -nodes -out %{__saml2_cert_file} -keyout %{__saml2_key_file}
    fi
fi

%preun
%systemd_preun %{__celery_service_name}.service
%systemd_preun %{__celerybeat_service_name}.service
%systemd_preun %{__uwsgi_service_name}.service

%postun
%systemd_postun_with_restart %{__celery_service_name}.service
%systemd_postun_with_restart %{__celerybeat_service_name}.service
%systemd_postun_with_restart %{__uwsgi_service_name}.service

%changelog
* Fri Sep 13 2019 Jenkins <jenkins@opennodecloud.com> - 3.9.7-1.el7
- New upstream release

* Thu Sep 12 2019 Jenkins <jenkins@opennodecloud.com> - 3.9.6-1.el7
- New upstream release

* Sat Aug 31 2019 Jenkins <jenkins@opennodecloud.com> - 3.9.5-1.el7
- New upstream release

* Mon Aug 26 2019 Jenkins <jenkins@opennodecloud.com> - 3.9.4-1.el7
- New upstream release

* Fri Aug 23 2019 Jenkins <jenkins@opennodecloud.com> - 3.9.3-1.el7
- New upstream release

* Wed Aug 21 2019 Jenkins <jenkins@opennodecloud.com> - 3.9.2-1.el7
- New upstream release

* Wed Aug 21 2019 Jenkins <jenkins@opennodecloud.com> - 3.9.1-1.el7
- New upstream release

* Mon Aug 19 2019 Jenkins <jenkins@opennodecloud.com> - 3.9.0-1.el7
- New upstream release

* Tue Aug 13 2019 Jenkins <jenkins@opennodecloud.com> - 3.8.9-1.el7
- New upstream release

* Tue Aug 13 2019 Jenkins <jenkins@opennodecloud.com> - 3.8.8-1.el7
- New upstream release

* Fri Aug 2 2019 Jenkins <jenkins@opennodecloud.com> - 3.8.7-1.el7
- New upstream release

* Thu Jul 25 2019 Jenkins <jenkins@opennodecloud.com> - 3.8.6-1.el7
- New upstream release

* Fri Jul 19 2019 Jenkins <jenkins@opennodecloud.com> - 3.8.5-1.el7
- New upstream release

* Tue Jul 16 2019 Jenkins <jenkins@opennodecloud.com> - 3.8.4-1.el7
- New upstream release

* Tue Jul 16 2019 Jenkins <jenkins@opennodecloud.com> - 3.8.3-1.el7
- New upstream release

* Sat Jul 13 2019 Jenkins <jenkins@opennodecloud.com> - 3.8.2-1.el7
- New upstream release

* Tue Jul 9 2019 Jenkins <jenkins@opennodecloud.com> - 3.8.1-1.el7
- New upstream release

* Mon Jul 8 2019 Jenkins <jenkins@opennodecloud.com> - 3.8.0-1.el7
- New upstream release

* Mon Jun 24 2019 Jenkins <jenkins@opennodecloud.com> - 3.7.9-1.el7
- New upstream release

* Fri Jun 21 2019 Jenkins <jenkins@opennodecloud.com> - 3.7.8-1.el7
- New upstream release

* Wed Jun 12 2019 Jenkins <jenkins@opennodecloud.com> - 3.7.7-1.el7
- New upstream release

* Wed Jun 12 2019 Jenkins <jenkins@opennodecloud.com> - 3.7.6-1.el7
- New upstream release

* Mon Jun 10 2019 Jenkins <jenkins@opennodecloud.com> - 3.7.5-1.el7
- New upstream release

* Wed Jun 5 2019 Jenkins <jenkins@opennodecloud.com> - 3.7.4-1.el7
- New upstream release

* Sat Jun 1 2019 Jenkins <jenkins@opennodecloud.com> - 3.7.3-1.el7
- New upstream release

* Wed May 29 2019 Jenkins <jenkins@opennodecloud.com> - 3.7.2-1.el7
- New upstream release

* Tue May 28 2019 Jenkins <jenkins@opennodecloud.com> - 3.7.1-1.el7
- New upstream release

* Tue May 28 2019 Jenkins <jenkins@opennodecloud.com> - 3.7.0-1.el7
- New upstream release

* Mon May 27 2019 Jenkins <jenkins@opennodecloud.com> - 3.6.9-1.el7
- New upstream release

* Sun May 26 2019 Jenkins <jenkins@opennodecloud.com> - 3.6.8-1.el7
- New upstream release

* Sat May 25 2019 Jenkins <jenkins@opennodecloud.com> - 3.6.7-1.el7
- New upstream release

* Thu May 23 2019 Jenkins <jenkins@opennodecloud.com> - 3.6.6-1.el7
- New upstream release

* Wed May 22 2019 Jenkins <jenkins@opennodecloud.com> - 3.6.5-1.el7
- New upstream release

* Sat May 18 2019 Jenkins <jenkins@opennodecloud.com> - 3.6.4-1.el7
- New upstream release

* Wed May 15 2019 Jenkins <jenkins@opennodecloud.com> - 3.6.3-1.el7
- New upstream release

* Sun May 12 2019 Jenkins <jenkins@opennodecloud.com> - 3.6.2-1.el7
- New upstream release

* Thu May 9 2019 Jenkins <jenkins@opennodecloud.com> - 3.6.1-1.el7
- New upstream release

* Thu May 9 2019 Jenkins <jenkins@opennodecloud.com> - 3.6.0-1.el7
- New upstream release

* Wed May 8 2019 Jenkins <jenkins@opennodecloud.com> - 3.5.9-1.el7
- New upstream release

* Wed May 8 2019 Jenkins <jenkins@opennodecloud.com> - 3.5.8-1.el7
- New upstream release

* Tue May 7 2019 Jenkins <jenkins@opennodecloud.com> - 3.5.7-1.el7
- New upstream release

* Mon May 6 2019 Jenkins <jenkins@opennodecloud.com> - 3.5.6-1.el7
- New upstream release

* Fri May 3 2019 Jenkins <jenkins@opennodecloud.com> - 3.5.5-1.el7
- New upstream release

* Thu May 2 2019 Jenkins <jenkins@opennodecloud.com> - 3.5.4-1.el7
- New upstream release

* Mon Apr 29 2019 Jenkins <jenkins@opennodecloud.com> - 3.5.3-1.el7
- New upstream release

* Sat Apr 27 2019 Jenkins <jenkins@opennodecloud.com> - 3.5.2-1.el7
- New upstream release

* Mon Apr 22 2019 Jenkins <jenkins@opennodecloud.com> - 3.5.1-1.el7
- New upstream release

* Wed Apr 17 2019 Jenkins <jenkins@opennodecloud.com> - 3.5.0-1.el7
- New upstream release

* Tue Apr 16 2019 Jenkins <jenkins@opennodecloud.com> - 3.4.9-1.el7
- New upstream release

* Fri Apr 12 2019 Jenkins <jenkins@opennodecloud.com> - 3.4.8-1.el7
- New upstream release

* Wed Apr 10 2019 Jenkins <jenkins@opennodecloud.com> - 3.4.7-1.el7
- New upstream release

* Tue Apr 9 2019 Jenkins <jenkins@opennodecloud.com> - 3.4.6-1.el7
- New upstream release

* Mon Apr 8 2019 Jenkins <jenkins@opennodecloud.com> - 3.4.5-1.el7
- New upstream release

* Fri Apr 5 2019 Jenkins <jenkins@opennodecloud.com> - 3.4.4-1.el7
- New upstream release

* Thu Apr 4 2019 Jenkins <jenkins@opennodecloud.com> - 3.4.3-1.el7
- New upstream release

* Thu Apr 4 2019 Jenkins <jenkins@opennodecloud.com> - 3.4.2-1.el7
- New upstream release

* Tue Mar 26 2019 Jenkins <jenkins@opennodecloud.com> - 3.4.1-1.el7
- New upstream release

* Tue Mar 19 2019 Jenkins <jenkins@opennodecloud.com> - 3.4.0-1.el7
- New upstream release

* Tue Mar 19 2019 Jenkins <jenkins@opennodecloud.com> - 3.3.9-1.el7
- New upstream release

* Mon Mar 11 2019 Jenkins <jenkins@opennodecloud.com> - 3.3.8-1.el7
- New upstream release

* Sun Mar 10 2019 Jenkins <jenkins@opennodecloud.com> - 3.3.7-1.el7
- New upstream release

* Thu Mar 7 2019 Jenkins <jenkins@opennodecloud.com> - 3.3.6-1.el7
- New upstream release

* Sat Mar 2 2019 Jenkins <jenkins@opennodecloud.com> - 3.3.5-1.el7
- New upstream release

* Thu Feb 28 2019 Jenkins <jenkins@opennodecloud.com> - 3.3.4-1.el7
- New upstream release

* Tue Feb 26 2019 Jenkins <jenkins@opennodecloud.com> - 3.3.3-1.el7
- New upstream release

* Tue Feb 19 2019 Jenkins <jenkins@opennodecloud.com> - 3.3.2-1.el7
- New upstream release

* Wed Feb 13 2019 Jenkins <jenkins@opennodecloud.com> - 3.3.1-1.el7
- New upstream release

* Mon Feb 11 2019 Jenkins <jenkins@opennodecloud.com> - 3.3.0-1.el7
- New upstream release

* Sat Feb 9 2019 Jenkins <jenkins@opennodecloud.com> - 3.2.9-1.el7
- New upstream release

* Sat Feb 9 2019 Jenkins <jenkins@opennodecloud.com> - 3.2.8-1.el7
- New upstream release

* Fri Feb 8 2019 Jenkins <jenkins@opennodecloud.com> - 3.2.7-1.el7
- New upstream release

* Tue Feb 5 2019 Jenkins <jenkins@opennodecloud.com> - 3.2.6-1.el7
- New upstream release

* Sun Jan 20 2019 Jenkins <jenkins@opennodecloud.com> - 3.2.5-1.el7
- New upstream release

* Sun Jan 6 2019 Jenkins <jenkins@opennodecloud.com> - 3.2.4-1.el7
- New upstream release

* Sat Dec 29 2018 Jenkins <jenkins@opennodecloud.com> - 3.2.3-1.el7
- New upstream release

* Wed Dec 26 2018 Jenkins <jenkins@opennodecloud.com> - 3.2.2-1.el7
- New upstream release

* Tue Dec 18 2018 Jenkins <jenkins@opennodecloud.com> - 3.2.1-1.el7
- New upstream release

* Fri Dec 14 2018 Jenkins <jenkins@opennodecloud.com> - 3.2.0-1.el7
- New upstream release

* Mon Dec 10 2018 Jenkins <jenkins@opennodecloud.com> - 3.1.9-1.el7
- New upstream release

* Fri Nov 30 2018 Jenkins <jenkins@opennodecloud.com> - 3.1.8-1.el7
- New upstream release

* Wed Nov 14 2018 Jenkins <jenkins@opennodecloud.com> - 3.1.7-1.el7
- New upstream release

* Sat Nov 10 2018 Jenkins <jenkins@opennodecloud.com> - 3.1.6-1.el7
- New upstream release

* Sat Nov 10 2018 Jenkins <jenkins@opennodecloud.com> - 3.1.5-1.el7
- New upstream release

* Fri Nov 2 2018 Jenkins <jenkins@opennodecloud.com> - 3.1.4-1.el7
- New upstream release

* Wed Oct 31 2018 Jenkins <jenkins@opennodecloud.com> - 3.1.3-1.el7
- New upstream release

* Tue Oct 30 2018 Jenkins <jenkins@opennodecloud.com> - 3.1.2-1.el7
- New upstream release

* Sun Oct 28 2018 Jenkins <jenkins@opennodecloud.com> - 3.1.1-1.el7
- New upstream release

* Tue Oct 23 2018 Jenkins <jenkins@opennodecloud.com> - 3.1.0-1.el7
- New upstream release

* Mon Oct 8 2018 Jenkins <jenkins@opennodecloud.com> - 3.0.9-1.el7
- New upstream release

* Mon Oct 1 2018 Jenkins <jenkins@opennodecloud.com> - 3.0.8-1.el7
- New upstream release

* Tue Aug 14 2018 Jenkins <jenkins@opennodecloud.com> - 3.0.7-1.el7
- New upstream release

* Sun Aug 12 2018 Jenkins <jenkins@opennodecloud.com> - 3.0.6-1.el7
- New upstream release

* Fri Aug 10 2018 Jenkins <jenkins@opennodecloud.com> - 3.0.5-1.el7
- New upstream release

* Thu Aug 9 2018 Jenkins <jenkins@opennodecloud.com> - 3.0.4-1.el7
- New upstream release

* Wed Aug 8 2018 Jenkins <jenkins@opennodecloud.com> - 3.0.3-1.el7
- New upstream release

* Tue Aug 7 2018 Jenkins <jenkins@opennodecloud.com> - 3.0.2-1.el7
- New upstream release

* Sat Aug 4 2018 Jenkins <jenkins@opennodecloud.com> - 3.0.1-1.el7
- New upstream release

* Thu Aug 2 2018 Jenkins <jenkins@opennodecloud.com> - 3.0.0-1.el7
- New upstream release

* Mon Jul 30 2018 Jenkins <jenkins@opennodecloud.com> - 2.9.9-1.el7
- New upstream release

* Wed Jul 25 2018 Jenkins <jenkins@opennodecloud.com> - 2.9.8-1.el7
- New upstream release

* Mon Jun 25 2018 Jenkins <jenkins@opennodecloud.com> - 2.9.7-1.el7
- New upstream release

* Mon Jun 25 2018 Jenkins <jenkins@opennodecloud.com> - 2.9.6-1.el7
- New upstream release

* Fri Jun 8 2018 Jenkins <jenkins@opennodecloud.com> - 2.9.5-1.el7
- New upstream release

* Fri Jun 1 2018 Jenkins <jenkins@opennodecloud.com> - 2.9.4-1.el7
- New upstream release

* Tue May 22 2018 Jenkins <jenkins@opennodecloud.com> - 2.9.3-1.el7
- New upstream release

* Mon May 14 2018 Jenkins <jenkins@opennodecloud.com> - 2.9.2-1.el7
- New upstream release

* Mon Apr 9 2018 Jenkins <jenkins@opennodecloud.com> - 2.9.1-1.el7
- New upstream release

* Sun Mar 25 2018 Jenkins <jenkins@opennodecloud.com> - 2.9.0-1.el7
- New upstream release

* Mon Feb 26 2018 Jenkins <jenkins@opennodecloud.com> - 2.8.9-1.el7
- New upstream release

* Sun Feb 18 2018 Jenkins <jenkins@opennodecloud.com> - 2.8.8-1.el7
- New upstream release

* Sun Feb 11 2018 Jenkins <jenkins@opennodecloud.com> - 2.8.7-1.el7
- New upstream release

* Sun Feb 4 2018 Jenkins <jenkins@opennodecloud.com> - 2.8.6-1.el7
- New upstream release

* Sat Jan 13 2018 Jenkins <jenkins@opennodecloud.com> - 2.8.5-1.el7
- New upstream release

* Fri Dec 22 2017 Jenkins <jenkins@opennodecloud.com> - 2.8.4-1.el7
- New upstream release

* Sun Dec 3 2017 Jenkins <jenkins@opennodecloud.com> - 2.8.3-1.el7
- New upstream release

* Mon Nov 27 2017 Jenkins <jenkins@opennodecloud.com> - 2.8.2-1.el7
- New upstream release

* Tue Nov 21 2017 Jenkins <jenkins@opennodecloud.com> - 2.8.1-1.el7
- New upstream release

* Wed Nov 8 2017 Jenkins <jenkins@opennodecloud.com> - 2.8.0-1.el7
- New upstream release

* Fri Nov 3 2017 Jenkins <jenkins@opennodecloud.com> - 2.7.9-1.el7
- New upstream release

* Tue Oct 17 2017 Jenkins <jenkins@opennodecloud.com> - 2.7.8-1.el7
- New upstream release

* Tue Oct 10 2017 Jenkins <jenkins@opennodecloud.com> - 2.7.7-1.el7
- New upstream release

* Wed Oct 4 2017 Jenkins <jenkins@opennodecloud.com> - 2.7.6-1.el7
- New upstream release

* Sat Sep 30 2017 Jenkins <jenkins@opennodecloud.com> - 2.7.4-1.el7
- New upstream release

* Thu Sep 28 2017 Jenkins <jenkins@opennodecloud.com> - 2.7.3-1.el7
- New upstream release

* Wed Sep 27 2017 Jenkins <jenkins@opennodecloud.com> - 2.7.2-1.el7
- New upstream release

* Sun Sep 17 2017 Jenkins <jenkins@opennodecloud.com> - 2.7.0-1.el7
- New upstream release

* Sat Aug 26 2017 Jenkins <jenkins@opennodecloud.com> - 2.6.8-1.el7
- New upstream release

* Sun Aug 6 2017 Jenkins <jenkins@opennodecloud.com> - 2.6.7-1.el7
- New upstream release

* Tue Aug 1 2017 Jenkins <jenkins@opennodecloud.com> - 2.6.6-1.el7
- New upstream release

* Mon Jul 17 2017 Jenkins <jenkins@opennodecloud.com> - 2.6.5-1.el7
- New upstream release

* Fri Jul 14 2017 Jenkins <jenkins@opennodecloud.com> - 2.6.4-1.el7
- New upstream release

* Wed Jul 12 2017 Jenkins <jenkins@opennodecloud.com> - 2.6.3-1.el7
- New upstream release

* Mon Jul 3 2017 Jenkins <jenkins@opennodecloud.com> - 2.6.2-1.el7
- New upstream release

* Fri Jun 30 2017 Jenkins <jenkins@opennodecloud.com> - 2.6.1-1.el7
- New upstream release

* Wed Jun 28 2017 Juri Hudolejev <juri@opennodecloud.com> - 2.6.0-1.el7
- Rename package to Waldur MasterMind
