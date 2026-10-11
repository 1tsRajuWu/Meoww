/**
 * premium.js — live embed preview for the Premium customizer page.
 *
 * No framework, no fetch, no Discord API calls. Every update is worked out
 * in the browser: variable substitution against demo values, color applied
 * as a CSS border, image shown/hidden on a valid URL.
 */

(function () {
  'use strict';

  // Demo values used for variable substitution in the live preview.
  const DEMO = {
    member_name: 'Peter',
    invites: 42,
    fake: 1,
    left: 3,
    rejoins: 2,
    server_name: 'Meoww Community',
    level: 12,
    xp: '4,800',
  };

  // Substitute {var} placeholders with demo values.
  function sub(text) {
    return text.replace(/\{(\w+)\}/g, function (match, key) {
      return Object.prototype.hasOwnProperty.call(DEMO, key) ? String(DEMO[key]) : match;
    });
  }

  // Bold numbers in substituted text.
  function boldNums(text) {
    // Wrap standalone numbers with a bold span
    return text.replace(/(\b\d[\d,]*\b)/g, '<strong style="color:#fff">$1</strong>');
  }

  // Build embed body HTML from description textarea lines.
  function buildBody(rawText) {
    if (!rawText.trim()) {
      return '<span style="color:#4e5058">No description yet.</span>';
    }
    const lines = rawText.split('\n');
    return lines.map(function (line, i) {
      if (!line.trim()) return '';
      const substituted = sub(line);
      const withBold = boldNums(substituted);
      const delay = (0.3 + i * 0.06).toFixed(2);
      return '<div class="prem-embed-line" style="animation-delay:' + delay + 's">' +
        '<span class="mw-arrow">\u25b8</span>' +
        '<span>' + withBold + '</span>' +
        '</div>';
    }).filter(Boolean).join('');
  }

  // Validate a loose URL (just needs a protocol and a dot).
  function looksLikeUrl(val) {
    return /^https?:\/\/.+\..+/.test(val.trim());
  }

  // Re-render the entire preview from current form state.
  function render() {
    // -- color --
    var color = colorInput.value || '#c374c1';
    embed.style.borderLeftColor = color;

    // -- title --
    var rawTitle = titleInput.value.trim();
    previewTitle.textContent = rawTitle ? sub(rawTitle) : 'Embed title';

    // -- body --
    previewBody.innerHTML = buildBody(descInput.value);

    // -- image --
    var imgVal = imageInput.value.trim();
    if (imgVal && looksLikeUrl(imgVal)) {
      previewImg.src = imgVal;
      previewImg.hidden = false;
    } else {
      previewImg.hidden = true;
      previewImg.src = '';
    }
  }

  // Insert text at the current cursor position in a textarea.
  function insertAtCursor(textarea, text) {
    var start = textarea.selectionStart;
    var end = textarea.selectionEnd;
    var before = textarea.value.slice(0, start);
    var after = textarea.value.slice(end);
    textarea.value = before + text + after;
    textarea.selectionStart = textarea.selectionEnd = start + text.length;
    textarea.focus();
    render();
  }

  // -- Element refs --
  var colorInput = document.getElementById('prem-color');
  var colorHex = document.getElementById('prem-hex');
  var titleInput = document.getElementById('prem-title');
  var descInput = document.getElementById('prem-desc');
  var imageInput = document.getElementById('prem-image');
  var embed = document.getElementById('prem-embed');
  var previewTitle = document.getElementById('prem-preview-title');
  var previewBody = document.getElementById('prem-preview-body');
  var previewImg = document.getElementById('prem-preview-img');

  if (!colorInput || !embed || !previewTitle || !previewBody || !previewImg) {
    // Not on the premium page; bail silently.
    return;
  }

  // -- Color picker: keep hex input in sync --
  colorInput.addEventListener('input', function () {
    colorHex.value = colorInput.value;
    render();
  });

  colorHex.addEventListener('input', function () {
    // Only apply if it looks like a valid 6-digit hex.
    if (/^#[0-9a-fA-F]{6}$/.test(colorHex.value)) {
      colorInput.value = colorHex.value;
    }
    render();
  });

  // -- Text fields --
  [titleInput, descInput, imageInput].forEach(function (el) {
    el.addEventListener('input', render);
  });

  // -- Variable pills: insert {var} at cursor in the description textarea --
  document.querySelectorAll('.mw-var-pill[data-var]').forEach(function (pill) {
    pill.addEventListener('click', function () {
      var varName = pill.dataset.var;
      if (varName) {
        insertAtCursor(descInput, '{' + varName + '}');
      }
    });
  });

  // -- Image error handling: hide if broken URL --
  previewImg.addEventListener('error', function () {
    previewImg.hidden = true;
  });

  // -- Initial render --
  render();

}());
