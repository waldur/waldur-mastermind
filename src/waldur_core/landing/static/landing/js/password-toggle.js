// Show or hide the value of an admin password field. Kept out of the widget
// template, so the admin's Content-Security-Policy needs no inline script.
// Every password widget includes this file; it binds once.
(function () {
  if (window.waldurPasswordToggleBound) {
    return;
  }
  window.waldurPasswordToggleBound = true;
  document.addEventListener('click', function (event) {
    const button = event.target.closest('.password-toggle');
    if (!button) {
      return;
    }
    const field = document.getElementById(button.dataset.targetId);
    if (!field) {
      return;
    }
    const hidden = field.type === 'password';
    field.type = hidden ? 'text' : 'password';
    button.textContent = hidden ? 'Hide' : 'Show';
  });
})();
