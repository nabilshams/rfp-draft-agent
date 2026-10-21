'use strict';
/* Escaping helpers for building HTML from untrusted text (RFP content, model output, server messages).
 * Kept free of DOM access so they can be unit tested with Node (see tests/test_ui_safety.py). */

const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

/** Escape text for use in HTML element content or a quoted attribute value. */
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => HTML_ESCAPES[c]);
}

/** The absolute URL if it is a plain http(s) link, otherwise ''. Blocks javascript:, data:, vbscript: and the like. */
function safeUrl(value, base) {
  try {
    const origin = base || (typeof location !== 'undefined' ? location.origin : 'http://localhost');
    const url = new URL(String(value ?? '').trim(), origin);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : '';
  } catch { return ''; }
}

if (typeof module !== 'undefined') module.exports = { esc, safeUrl };
