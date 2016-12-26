Name: nodeconductor-assembly-waldur
Summary: NodeConductor assembly for Waldur project
Group: Development/Libraries
Version: 2.0.0
Release: 1.el7
License: MIT
Url: http://nodeconductor.com
Source0: %{name}-%{version}.tar.gz

Requires: nodeconductor > 0.112.0
Requires: nodeconductor-auth-social >= 0.1.0
Requires: nodeconductor-aws >= 0.1.3
Requires: nodeconductor-digitalocean >= 0.1.4
Requires: nodeconductor-openstack >= 0.11.0
Requires: python-jira >= 1.0.7

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
* Fri Dec 23 2016 Jenkins <jenkins@opennodecloud.com> - 2.0.0-1.el7
- New upstream release

* Mon Aug 22 2016 Juri Hudolejev <juri@opennodecloud.com> - 0.1.0-1.el7
- Initial version of the package

