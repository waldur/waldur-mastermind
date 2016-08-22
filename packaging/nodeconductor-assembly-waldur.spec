Name: nodeconductor-assembly-waldur
Summary: NodeConductor assembly for Waldur project
Group: Development/Libraries
Version: 0.1.0
Release: 1.el7
License: Copyright 2016 OpenNode LLC. All rights reserved.
Url: http://nodeconductor.com
Source0: %{name}-%{version}.tar.gz

Requires: nodeconductor
Requires: nodeconductor-jira
Requires: nodeconductor-openid
Requires: nodeconductor-openstack
Requires: nodeconductor-organization
Requires: nodeconductor-paypal
Requires: nodeconductor-plus


BuildArch: noarch
BuildRoot: %{_tmppath}/%{name}-%{version}-%{release}-buildroot

BuildRequires: python-setuptools

%description
NodeConductor assembly for Waldur project.

%prep
%setup -q -n %{name}-%{version}

%build
python setup.py build

%install
rm -rf %{buildroot}
%{__python} setup.py install -O1 --root=%{buildroot}

%clean
rm -rf %{buildroot}

%files
%defattr(-,root,root)
%{python_sitelib}/*

%defattr(-,root,root)

%changelog
* Mon Aug 22 2016 Juri Hudolejev <juri@opennodecloud.com> - 0.1.0-1.el7
- Initial version of the package

