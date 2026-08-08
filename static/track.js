/*! Behavioral Funnel Agent — browser tracking snippet.
 *
 *  Add to your site (once, before </body>):
 *    <script src="https://YOUR_APP_HOST/track.js"
 *            data-api="https://YOUR_APP_HOST"
 *            data-key="YOUR_WRITE_KEY"
 *            data-site="default"></script>
 *
 *  It auto-tracks page views (incl. SPA route changes), clicks, and ENGAGEMENT —
 *  active (non-idle, tab-visible) seconds per page, scroll depth, and seconds
 *  spent inside each section. Mark sections you care about with
 *    <section data-fa-section="pricing-table"> ... </section>
 *  (any `section[id]` / `[data-section]` is picked up automatically too).
 *
 *  Location: every visitor gets an approximate city from their IP automatically.
 *  That is the ISP's gateway, not the person — on mobile networks it is often the
 *  wrong city. For a real street-level location the visitor must opt in:
 *    <button data-fa-locate>Find my nearest clinic</button>   (or funnel.locate())
 *  which shows the browser's own permission prompt. There is no silent way.
 *
 *  Programmatic API:
 *    window.funnel.track('cta_click', { metadata: { id: 'hero' } });
 *    window.funnel.identify({ email: 'a@b.com', email_opt_in: true,
 *                             whatsapp_opt_in: false, consent_source: 'newsletter' });
 *    window.funnel.locate().then(function (r) { ... });   // r.ok, r.accuracy_m
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
    try { o.tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) {}
    if (navigator.language) o.lang = navigator.language;
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

  // A "visit" = one page view. Every event carries its visit id (vid) so the
  // server can attribute clicks and dwell to the exact page view they happened
  // on — that's what makes "45s on /pricing" a real, per-visit measurement.
  var visitId = 'v-' + uuid();

  function track(event_type, opts) {
    opts = opts || {};
    var meta = Object.assign({ title: document.title, vid: visitId }, utm(), opts.metadata || {});
    post('/track', {
      event_type: event_type,
      url: opts.url || location.href,
      timestamp: new Date().toISOString(),
      anonymous_id: anonId(),
      session_id: sessId(),
      metadata: meta
    });
  }

  // Ask the browser for a precise location. This ALWAYS shows the native
  // permission prompt — there is no way to get it silently, and there shouldn't
  // be. Call it from a click the visitor understands ("find my nearest clinic"),
  // never on page load: an unexplained prompt gets denied, and a denial is
  // remembered by the browser for that origin.
  //   window.funnel.locate().then(function (r) { ... })
  function locate(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      if (!navigator.geolocation) { resolve({ ok: false, reason: 'unsupported' }); return; }
      navigator.geolocation.getCurrentPosition(
        function (pos) {
          var c = pos.coords;
          post('/locate', {
            anonymous_id: anonId(),
            latitude: c.latitude,
            longitude: c.longitude,
            accuracy_m: c.accuracy
          });
          resolve({ ok: true, latitude: c.latitude, longitude: c.longitude, accuracy_m: c.accuracy });
        },
        function (err) { resolve({ ok: false, reason: err && err.code === 1 ? 'denied' : 'unavailable' }); },
        {
          enableHighAccuracy: opts.highAccuracy !== false,
          timeout: opts.timeout || 10000,
          maximumAge: opts.maximumAge || 300000
        }
      );
    });
  }

  function identify(traits) {
    traits = traits || {};
    post('/identify', {
      anonymous_id: anonId(),
      name: traits.name || null,
      email: traits.email || null,
      phone: traits.phone || null,
      email_opt_in: !!traits.email_opt_in,
      whatsapp_opt_in: !!traits.whatsapp_opt_in,
      consent_timestamp: traits.consent_timestamp || new Date().toISOString(),
      consent_source: traits.consent_source || null
    });
  }

  // ---------------------------------------------------------------------------
  // Engagement — ACTIVE time, not wall-clock time.
  //
  // A second is counted only when the tab is visible AND the visitor interacted
  // (move / scroll / key / touch) within IDLE_MS. So a pricing tab left open in
  // the background for an hour scores zero, while 45 focused seconds score 45.
  // Time is attributed to whichever sections are on screen at that moment.
  // Deltas are flushed as `page_engagement` events — never absolute totals — so
  // the server can simply sum them.
  // ---------------------------------------------------------------------------
  var TICK_MS = 1000;        // resolution of the active-time counter
  var IDLE_MS = 60000;       // no interaction for this long => idle, stop counting
  var FLUSH_MS = 30000;      // flush once this much *active* time has accrued
  var MAX_SECTIONS = 40;     // safety cap on section fan-out per page

  var eng = null;            // current visit's engagement accumulator
  var lastInteraction = Date.now();
  var visibleSections = {};  // section name -> true while intersecting

  function newEngagement() {
    return { activeMs: 0, sentMs: 0, sections: {}, sentSections: {}, maxScroll: 0, startedAt: Date.now() };
  }

  function scrollPct() {
    var h = document.documentElement, b = document.body;
    var total = Math.max(h.scrollHeight || 0, b ? b.scrollHeight : 0) - window.innerHeight;
    if (total <= 0) return 100;
    return Math.max(0, Math.min(100, Math.round((window.pageYOffset || h.scrollTop || 0) / total * 100)));
  }

  function sectionName(el, i) {
    var n = el.getAttribute('data-fa-section') || el.getAttribute('data-section') || el.id || ('section-' + i);
    return String(n).replace(/\s+/g, ' ').trim().slice(0, 60);
  }

  // Section visibility is measured with plain geometry on each tick rather than
  // an IntersectionObserver: one rect read per section per second is cheap, and
  // it can't silently go quiet the way an observer can when a section is added,
  // moved or re-rendered after the initial scan.
  var tracked = [];
  function observeSections() {
    tracked = [];
    var els = document.querySelectorAll('[data-fa-section], [data-section], section[id]');
    for (var i = 0; i < els.length && i < MAX_SECTIONS; i++) {
      tracked.push({ el: els[i], name: sectionName(els[i], i) });
    }
  }

  // On screen = a third of the section is in view, or it fills half the viewport
  // (so a very tall section still counts while you read the middle of it).
  function onScreen(el) {
    var r = el.getBoundingClientRect();
    var vh = window.innerHeight || document.documentElement.clientHeight;
    var visible = Math.min(r.bottom, vh) - Math.max(r.top, 0);
    if (visible <= 0) return false;
    return visible >= r.height * 0.35 || visible >= vh * 0.5;
  }

  function tick() {
    if (!eng) return;
    if (document.visibilityState !== 'visible') return;
    if (Date.now() - lastInteraction > IDLE_MS) return;   // idle: don't count it
    eng.activeMs += TICK_MS;
    visibleSections = {};
    for (var i = 0; i < tracked.length; i++) {
      var t = tracked[i];
      if (!t.el.isConnected) continue;
      try { if (!onScreen(t.el)) continue; } catch (e) { continue; }
      visibleSections[t.name] = true;
      eng.sections[t.name] = (eng.sections[t.name] || 0) + TICK_MS;
    }
    eng.maxScroll = Math.max(eng.maxScroll, scrollPct());
    if (eng.activeMs - eng.sentMs >= FLUSH_MS) flushEngagement();
  }

  function flushEngagement(url) {
    if (!eng) return;
    var deltaMs = eng.activeMs - eng.sentMs;
    var sections = {};
    var hasSections = false;
    for (var name in eng.sections) {
      if (!Object.prototype.hasOwnProperty.call(eng.sections, name)) continue;
      var d = eng.sections[name] - (eng.sentSections[name] || 0);
      if (d > 0) { sections[name] = d; hasSections = true; }
    }
    if (deltaMs < 1000 && !hasSections) return;   // nothing meaningful to report

    eng.sentMs = eng.activeMs;
    for (var k in eng.sections) {
      if (Object.prototype.hasOwnProperty.call(eng.sections, k)) eng.sentSections[k] = eng.sections[k];
    }
    track('page_engagement', {
      url: url || location.href,
      metadata: {
        active_ms: deltaMs,
        elapsed_ms: Date.now() - eng.startedAt,
        scroll_pct: eng.maxScroll,
        sections: sections
      }
    });
  }

  window.funnel = {
    track: track, identify: identify, locate: locate, anonymousId: anonId,
    flush: function () { flushEngagement(); },
    // Call after rendering new sections (SPA views, lazy content) to re-scan.
    scanSections: observeSections
  };

  if (cfg.auto) {
    var currentUrl = location.href;
    var routeKey = function () { return location.pathname + location.search; };
    var currentRoute = routeKey();
    var pageview = function (force) {
      // Jumping to an in-page anchor (#pricing) is the same page — Chrome fires
      // popstate for it, and treating that as a new visit would reset the dwell
      // clock every time someone uses the nav. Only a real route change counts.
      if (!force && eng && routeKey() === currentRoute) { setTimeout(observeSections, 0); return; }
      // Close out the previous page before the URL changes, so its dwell is
      // attributed to the page it was actually spent on.
      flushEngagement(currentUrl);
      currentUrl = location.href;
      currentRoute = routeKey();
      visitId = 'v-' + uuid();
      eng = newEngagement();
      lastInteraction = Date.now();
      track('page_view');
      setTimeout(observeSections, 0);
    };
    // Listeners are wrapped so the event object never lands in `force`.
    if (document.readyState !== 'loading') pageview(true);
    else document.addEventListener('DOMContentLoaded', function () { pageview(true); });

    // SPA route changes
    ['pushState', 'replaceState'].forEach(function (m) {
      var orig = history[m];
      history[m] = function () {
        var r = orig.apply(this, arguments);
        setTimeout(function () { pageview(); }, 0);
        return r;
      };
    });
    window.addEventListener('popstate', function () { pageview(); });

    // Interaction = proof the visitor is actually there (drives the idle gate).
    ['mousemove', 'mousedown', 'keydown', 'scroll', 'touchstart', 'wheel', 'focus'].forEach(function (ev) {
      window.addEventListener(ev, function () { lastInteraction = Date.now(); }, { passive: true, capture: true });
    });

    setInterval(tick, TICK_MS);
    // Flush whatever is pending whenever the visitor leaves or hides the tab.
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') flushEngagement();
      else lastInteraction = Date.now();
    });
    window.addEventListener('pagehide', function () { flushEngagement(); });
    window.addEventListener('beforeunload', function () { flushEngagement(); });

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

      // Declarative precise-location opt-in: <button data-fa-locate>Find my
      // nearest clinic</button>. Tied to a real click so the visitor sees the
      // prompt in a context that explains why it's being asked.
      if (el.hasAttribute && el.hasAttribute('data-fa-locate')) locate();
    }, true);

    // Presence heartbeat — powers the dashboard "Live now" view. Lightweight,
    // visibility-aware (paused when the tab is hidden), and stored as presence
    // only (never an event), so it can't bloat history or skew scoring.
    var HEARTBEAT_MS = 10000;
    function heartbeat() { if (document.visibilityState === 'visible') track('heartbeat'); }
    setInterval(heartbeat, HEARTBEAT_MS);
    document.addEventListener('visibilitychange', heartbeat);
  }
})();
