Name: waldur-mastermind
Summary: Waldur MasterMind
Group: Development/Libraries
Version: 2.8.7
Release: 1.el7
License: MIT
Url: https://waldur.com
Source0: %{name}-%{version}.tar.gz

Requires: ansible-waldur-module >= 0.5.0
Requires: waldur-core >= 0.155.0
Requires: waldur-ansible >= 0.3.3
Requires: waldur-auth-openid >= 0.8.7
Requires: waldur-auth-social >= 0.7.8
Requires: waldur-auth-saml2 >= 0.8.6
Requires: waldur-auth-valimo >= 0.3.1
Requires: waldur-aws >= 0.11.2
Requires: waldur-azure >= 0.3.4
Requires: waldur-cost-planning >= 0.6.1
Requires: waldur-digitalocean >= 0.10.2
Requires: waldur-freeipa >= 0.6.3
Requires: waldur-openstack >= 0.40.3
Requires: waldur-paypal >= 0.7.0
Requires: waldur-slurm >= 0.4.1
Requires: waldur-zabbix >= 0.7.3
Requires: python2-defusedxml >= 0.4.1
Requires: python-django >= 1.11, python-django < 2.0
Requires: python-django-auth-ldap >= 1.3.0
Requires: python-influxdb >= 4.1.0
Requires: python-jira >= 1.0.7
Requires: python2-pdfkit >= 0.6.1

Obsoletes: nodeconductor-assembly-waldur

BuildArch: noarch
BuildRoot: %{_tmppath}/%{name}-%{version}-%{release}-buildroot

BuildRequires: python-setuptools

%description
Waldur MasterMind is part of the Waldur suite - https://waldur.com - for
managing of hybrid cloud resources. It is used to control both internal
enterprise IT resources and for selling cloud services to the public.

%prep
%setup -q -n %{name}-%{version}

%build
%{__python} setup.py build

%install
rm -rf %{buildroot}
%{__python} setup.py install -O1 --root=%{buildroot}

install -d %{buildroot}%{_bindir}
install packaging/usr/bin/%{name}-check %{buildroot}%{_bindir}

%clean
rm -rf %{buildroot}

%files
%defattr(-,root,root)
%{python_sitelib}/*
%{_bindir}/*

%defattr(-,root,root)

%changelog
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
