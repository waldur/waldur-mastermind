// Go to the choice picked in an admin dropdown list filter. Kept out of the
// filter template, so the admin's Content-Security-Policy needs no inline
// script. Every dropdown filter includes this file; it binds once.
(function () {
  if (window.waldurDropdownFilterBound) {
    return;
  }
  window.waldurDropdownFilterBound = true;
  document.addEventListener('change', function (event) {
    const select = event.target;
    if (!select.matches || !select.matches('select[data-dropdown-filter]')) {
      return;
    }
    window.location = window.location.pathname + select.value;
  });
})();
