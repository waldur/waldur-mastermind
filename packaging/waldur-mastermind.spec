Name: waldur-mastermind
Summary: Waldur MasterMind
Group: Development/Libraries
Version: 2.6.1
Release: 1.el7
License: MIT
Url: https://waldur.com
Source0: %{name}-%{version}.tar.gz

Requires: waldur-core > 0.141.1
Requires: nodeconductor-auth-social >= 0.7.1
Requires: nodeconductor-auth-openid >= 0.8.2
Requires: nodeconductor-aws >= 0.9.1
Requires: nodeconductor-cost-planning >= 0.4.1
Requires: nodeconductor-digitalocean >= 0.8.1
Requires: nodeconductor-openstack >= 0.30.1
Requires: nodeconductor-saml2 >= 0.7.2
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
* Fri Jun 30 2017 Jenkins <jenkins@opennodecloud.com> - 2.6.1-1.el7
- New upstream release

* Wed Jun 28 2017 Juri Hudolejev <juri@opennodecloud.com> - 2.6.0-1.el7
- Rename package to Waldur MasterMind
