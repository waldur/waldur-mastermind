function copyToClipboard(text) {
  const hiddenDiv = document.createElement('div');
  const style = hiddenDiv.style;
  style.height = '1px';
  style.width = '1px';
  style.overflow = 'hidden';
  style.position = 'fixed';
  style.top = '0px';
  style.left = '0px';

  const textarea = document.createElement('textarea');
  textarea.readOnly = true;
  textarea.value = text;

  hiddenDiv.appendChild(textarea);
  document.body.appendChild(hiddenDiv);

  textarea.select();
  document.execCommand('copy');
  document.body.removeChild(hiddenDiv);
}

django.jQuery(function ($) {
    django.jQuery('.copy-button').click(function() {
      copyToClipboard(django.jQuery('#' + django.jQuery(this).data('target-id')).val());
    });
});
