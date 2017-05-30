Name: python-waldur-client
Summary: Waldur Client for OpenStack infrastructure management.
Group: Development/Libraries
Version: 0.0.1
Release: 1.el7
License: MIT
Url: https://waldur.com
Source0: %{name}-%{version}.tar.gz

Requires: python-requests >= 2.6.0

BuildArch: noarch
BuildRoot: %{_tmppath}/%{name}-%{version}-%{release}-buildroot

BuildRequires: python-setuptools

%description
Waldur Client for OpenStack infrastructure management.

%prep
%setup -q -n %{name}-%{version}

%build
%{__python} setup.py build

%install
rm -rf %{buildroot}
%{__python} setup.py install -O1 --root=%{buildroot}

%clean
rm -rf %{buildroot}

%files
%defattr(-,root,root,-)
%{python_sitelib}/*
