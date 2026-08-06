/*! Behavioral Funnel Agent — browser tracking snippet.
 *
 *  Add to your site (once, before </body>):
 *    <script src="https://YOUR_APP_HOST/track.js"
 *            data-api="https://YOUR_APP_HOST"
 *            data-key="YOUR_WRITE_KEY"
 *            data-site="default"></script>
 *
 *  It auto-tracks page views (incl. SPA route changes) and clicks on any element
 *  with a data-fa-event attribute. Programmatic API:
 *    window.funnel.track('cta_click', { metadata: { id: 'hero' } });
 *    window.funnel.identify({ email: 'a@b.com', email_opt_in: true,
 *                             whatsapp_opt_in: false, consent_source: 'newsletter' });
 *
 *  The write key is public (it ships in the browser) — it identifies the source
 *  and enables rotation / rate-limiting, not secrecy. The LLM key never touches
 *  the browser; scoring & decisions happen server-side.
 */
(function () {
  var s = document.currentScript ||
    (function () { var e = document.getElementsByTagName('script'); return e[e.length - 1]; })();
  var cfg = {
    api:  (s && s.getAttribute('data-api'))  || window.FUNNEL_API || '',   // '' = same origin
    key:  (s && s.getAttribute('data-key'))  || window.FUNNEL_KEY || '',
    site: (s && s.getAttribute('data-site')) || 'default',
    auto: !(s && s.getAttribute('data-auto') === 'off')
  };

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    });
  }
  function getCookie(n) { var m = document.cookie.match('(?:^|; )' + n + '=([^;]*)'); return m ? decodeURIComponent(m[1]) : null; }
  function setCookie(n, v, days) {
    var d = new Date(Date.now() + days * 864e5);
    document.cookie = n + '=' + encodeURIComponent(v) + '; expires=' + d.toUTCString() + '; path=/; SameSite=Lax';
  }

  function anonId() { var id = getCookie('fa_anon'); if (!id) { id = 'anon-' + uuid(); setCookie('fa_anon', id, 365); } return id; }
  function sessId() {
    try { var id = sessionStorage.getItem('fa_sess'); if (!id) { id = 's-' + uuid(); sessionStorage.setItem('fa_sess', id); } return id; }
    catch (e) { return 's-' + uuid(); }
  }
  function utm() {
    var q = new URLSearchParams(location.search), o = {};
    ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content'].forEach(function (k) { if (q.get(k)) o[k] = q.get(k); });
    if (document.referrer) o.referrer = document.referrer;
    return o;
  }

  function post(path, bodyObj) {
    var headers = { 'Content-Type': 'application/json', 'X-Site-Id': cfg.site };
    if (cfg.key) headers['X-Write-Key'] = cfg.key;
    try {
      fetch((cfg.api || '') + path, {
        method: 'POST', headers: headers, body: JSON.stringify(bodyObj),
        keepalive: true, mode: 'cors'
      }).catch(function () {});
    } catch (e) {}
  }

  function track(event_type, opts) {
    opts = opts || {};
    var meta = Object.assign({ title: document.title }, utm(), opts.metadata || {});
    post('/track', {
      event_type: event_type,
      url: opts.url || location.href,
      timestamp: new Date().toISOString(),
      anonymous_id: anonId(),
      session_id: sessId(),
      metadata: meta
    });
  }

  function identify(traits) {
    traits = traits || {};
    post('/identify', {
      anonymous_id: anonId(),
      email: traits.email || null,
      phone: traits.phone || null,
      email_opt_in: !!traits.email_opt_in,
      whatsapp_opt_in: !!traits.whatsapp_opt_in,
      consent_timestamp: traits.consent_timestamp || new Date().toISOString(),
      consent_source: traits.consent_source || null
    });
  }

  window.funnel = { track: track, identify: identify, anonymousId: anonId };

  if (cfg.auto) {
    var pageview = function () { track('page_view'); };
    if (document.readyState !== 'loading') pageview();
    else document.addEventListener('DOMContentLoaded', pageview);

    // SPA route changes
    ['pushState', 'replaceState'].forEach(function (m) {
      var orig = history[m];
      history[m] = function () { var r = orig.apply(this, arguments); setTimeout(pageview, 0); return r; };
    });
    window.addEventListener('popstate', pageview);

    // Click tracking — declarative (data-fa-event) AND generic (links/buttons),
    // so meaningful clicks are captured even without any markup on the site.
    document.addEventListener('click', function (e) {
      var t = e.target;
      var el = t && t.closest && t.closest('[data-fa-event], a, button, [role="button"], input[type="submit"], input[type="button"]');
      if (!el) return;
      var custom = el.getAttribute('data-fa-event');
      var text = (el.innerText || el.textContent || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 80);
      var href = el.tagName === 'A' ? (el.getAttribute('href') || null) : null;
      var meta = {
        tag: (el.tagName || '').toLowerCase(),
        text: text,
        id: el.getAttribute('data-fa-id') || el.id || null,
        href: href
      };
      track(custom || 'click', { metadata: meta });
    }, true);

    // Presence heartbeat — powers the dashboard "Live now" view. Lightweight,
    // visibility-aware (paused when the tab is hidden), and stored as presence
    // only (never an event), so it can't bloat history or skew scoring.
    var HEARTBEAT_MS = 15000;
    function heartbeat() { if (document.visibilityState === 'visible') track('heartbeat'); }
    setInterval(heartbeat, HEARTBEAT_MS);
    document.addEventListener('visibilitychange', heartbeat);
  }
})();
