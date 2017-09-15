Name: waldur-mastermind
Summary: Waldur MasterMind
Group: Development/Libraries
Version: 2.6.8
Release: 1.el7
License: MIT
Url: https://waldur.com
Source0: %{name}-%{version}.tar.gz

Requires: waldur-core >= 0.146.4
Requires: waldur-ansible >= 0.1.0
Requires: waldur-auth-openid >= 0.8.3
Requires: waldur-auth-social >= 0.7.2
Requires: waldur-auth-saml2 >= 0.7.3
Requires: waldur-aws >= 0.10.0
Requires: waldur-azure >= 0.3.0
Requires: waldur-cost-planning >= 0.4.2
Requires: waldur-digitalocean >= 0.8.3
Requires: waldur-freeipa >= 0.2.2
Requires: waldur-openstack >= 0.34.0
Requires: waldur-paypal >= 0.6.0
Requires: waldur-slurm >= 0.1.3
Requires: python2-defusedxml == 0.4.1
Requires: python-influxdb >= 4.1.0
Requires: python-jira >= 1.0.7

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
