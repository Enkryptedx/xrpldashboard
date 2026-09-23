/* Shared live XRPL ledger stream. One WebSocket per page, many subscribers.
 *
 * The XRPL `ledger` stream pushes a `ledgerClosed` event every time a
 * ledger is validated (~3.5s cadence). Any panel that wants to react to
 * a real ledger close — the liveness chip, the home ticker, the /health
 * heartbeat — subscribes via LiveStream.onLedger(cb). No polling, no
 * server hop, no fake ticks.
 *
 * Why browser-direct WS instead of server-mediated SSE: the public ledger
 * stream is free, low-volume (~17 events/min), and CSP already allows
 * wss://xrplcluster.com. Going through Flask would tie up gunicorn workers
 * for every connected tab on the Render free tier. This is cheaper and
 * lower-latency for the panels that just need "did a ledger just close."
 *
 * Own-node primary (Charlie ruling 2026-09-22 Tue PM): when the template
 * injects window.LIVE_STREAM_WSS_PRIMARY (differs from fallback), that
 * URL is prepended to the connect list. A ~10s watchdog kicks in when
 * we connect to the primary; if no ledgerClosed arrives in that window,
 * the socket is closed and we step to the next URL. The first time we
 * end up connected to anything other than the primary (or gave up on
 * it), we show a small amber "using fallback" banner and ping
 * /api/walker-node-fallback once per session. Successful re-connect to
 * the primary hides the banner.
 *
 * Anything that needs server-derived data (load_factor, XRP/USD oracle,
 * Postgres-backed counters) still polls the JSON API on a slower cadence.
 */
(function (window) {
  'use strict';
  if (window.LiveStream) return;

  // Bundled URL list. CSP allows all three (see _CSP_CONNECT_SRC in app.py).
  // If window.LIVE_STREAM_WSS_PRIMARY is set (and differs), it is prepended.
  var WS_URLS = [
    'wss://xrplcluster.com',
    'wss://s2.ripple.com',
    'wss://s1.ripple.com'
  ];

  var PRIMARY_URL = (window.LIVE_STREAM_WSS_PRIMARY || '').trim();
  var PRIMARY_LABEL = window.LIVE_STREAM_WSS_PRIMARY_LABEL || '';
  var FALLBACK_LABEL = window.LIVE_STREAM_WSS_FALLBACK_LABEL || 'public XRPL cluster';
  if (PRIMARY_URL && WS_URLS.indexOf(PRIMARY_URL) === -1) {
    WS_URLS.unshift(PRIMARY_URL);
  }

  // Watchdog for primary: if the socket opens on the primary but no
  // ledgerClosed arrives by this deadline, treat as failed and step to
  // the next URL. Bumped 2026-09-23 17:15 ET (Charlie ruling) from 10s
  // to 15s — the 10s window was too tight for cold Cloudflare-tunnel
  // handshakes, tripping ~24 false-positive fallback events / 24h.
  var PRIMARY_LEDGER_WATCHDOG_MS = 15000;

  // Background primary-retry interval — while on a fallback URL, quietly
  // attempt to reconnect to the primary every N ms. If it succeeds + a
  // ledger arrives, we switch back and clear the fallback banner.
  var PRIMARY_RETRY_MS = 30000;

  var urlIdx = 0;
  var ws = null;
  var connected = false;
  var closed = false;
  var reconnectDelay = 1500;
  var MAX_RECONNECT_DELAY = 30000;

  var ledgerListeners = [];
  var statusListeners = [];

  var lastLedger = null;
  var lastLedgerReceivedAt = null;

  var primaryWatchdogTimer = null;
  var primaryRetryTimer = null;
  var fallbackPinged = false;
  var bannerEl = null;

  // Three-state banner sequence (Charlie ruling 2026-09-23 17:15 ET):
  //   'connecting'  → "Connecting to our own node…"
  //   'fallback'    → "Our node is still connecting — showing the public feed until it does"
  //   'hidden'      → banner removed (primary is live)
  // Never lead with the fallback. Start in 'connecting' the moment the
  // page loads (if PRIMARY_URL is configured); flip to 'fallback' only
  // if the watchdog trips AND we've actually landed on a non-primary URL.
  var bannerState = 'hidden';

  function currentUrl() {
    return WS_URLS[urlIdx % WS_URLS.length];
  }

  function isOnPrimary() {
    return !!PRIMARY_URL && currentUrl() === PRIMARY_URL;
  }

  function clearWatchdog() {
    if (primaryWatchdogTimer) {
      clearTimeout(primaryWatchdogTimer);
      primaryWatchdogTimer = null;
    }
  }

  function clearPrimaryRetry() {
    if (primaryRetryTimer) {
      clearTimeout(primaryRetryTimer);
      primaryRetryTimer = null;
    }
  }

  var BANNER_STYLE = [
    'position:fixed', 'left:12px', 'bottom:12px', 'z-index:2147483000',
    'padding:8px 12px', 'border-radius:6px',
    'background:#ffb020', 'color:#1a1400',
    'font:12px/1.4 system-ui,-apple-system,Segoe UI,sans-serif',
    'box-shadow:0 2px 8px rgba(0,0,0,0.15)',
    'max-width:320px', 'cursor:default'
  ].join(';');

  function setBanner(state) {
    if (typeof document === 'undefined') return;
    if (state === bannerState) return;
    bannerState = state;
    if (state === 'hidden') {
      if (bannerEl && bannerEl.parentNode) {
        bannerEl.parentNode.removeChild(bannerEl);
      }
      bannerEl = null;
      return;
    }
    var text = state === 'connecting'
      ? 'Connecting to our own node…'
      : 'Our node is still connecting — showing the public feed until it does';
    var dataAttr = state === 'connecting'
      ? 'connecting'
      : 'fallback';
    if (!bannerEl) {
      var el = document.createElement('div');
      el.setAttribute('role', 'status');
      el.setAttribute('data-live-stream-banner', dataAttr);
      el.style.cssText = BANNER_STYLE;
      el.textContent = text;
      if (document.body) {
        document.body.appendChild(el);
        bannerEl = el;
      } else {
        document.addEventListener('DOMContentLoaded', function () {
          if (bannerState !== 'hidden' && !bannerEl && document.body) {
            document.body.appendChild(el);
            bannerEl = el;
          }
        });
      }
    } else {
      bannerEl.setAttribute('data-live-stream-banner', dataAttr);
      bannerEl.textContent = text;
    }
  }

  // Preserve the old API name for any legacy caller — routes through the
  // new state machine so we never fire the fallback banner spuriously.
  function hideFallbackBanner() { setBanner('hidden'); }

  function pingFallbackTelemetry(reason) {
    // Only fire once per session, and only after we've actually stepped
    // to a non-primary URL — logging primary→primary was the bulk of
    // the false-positive events last 24h.
    if (fallbackPinged) return;
    if (!PRIMARY_URL) return;
    var fallbackUrl = currentUrl();
    if (fallbackUrl === PRIMARY_URL) return; // still on primary — skip
    fallbackPinged = true;
    try {
      var params = new URLSearchParams({
        source: 'browser_wss',
        primary: PRIMARY_URL,
        fallback: fallbackUrl,
        reason: reason || ''
      });
      var url = '/api/walker-node-fallback?' + params.toString();
      if (navigator && typeof navigator.sendBeacon === 'function') {
        navigator.sendBeacon(url);
      } else {
        fetch(url, { method: 'POST', keepalive: true }).catch(function () {});
      }
    } catch (e) {}
  }

  // Called after a successful step to a fallback URL AND its first
  // ledger — this is when the "fallback banner" state is genuinely
  // correct. Callers who fire ON PRIMARY (watchdog trip, onerror)
  // must NOT call this — they should just close + step, and this fires
  // when the fallback URL's ledger actually lands.
  function noteFallbackLive(reason) {
    setBanner('fallback');
    pingFallbackTelemetry(reason);
    // Background retry to the primary — every PRIMARY_RETRY_MS, try to
    // reconnect. If the retry succeeds + a ledger arrives, we'll switch
    // back and clear the banner.
    if (PRIMARY_URL && !primaryRetryTimer) {
      primaryRetryTimer = setInterval(function () {
        if (closed || isOnPrimary()) { clearPrimaryRetry(); return; }
        // Reset urlIdx to primary; close current; reconnect will pick it up.
        urlIdx = WS_URLS.indexOf(PRIMARY_URL);
        if (ws) { try { ws.close(); } catch (e) {} }
      }, PRIMARY_RETRY_MS);
    }
  }

  function fireLedger(data) {
    lastLedger = data;
    lastLedgerReceivedAt = Date.now();
    for (var i = 0; i < ledgerListeners.length; i++) {
      try { ledgerListeners[i](data); } catch (e) {}
    }
  }

  function fireStatus(s) {
    for (var i = 0; i < statusListeners.length; i++) {
      try { statusListeners[i](s); } catch (e) {}
    }
  }

  // Called on the FIRST ledger arrival for a given ws.onopen — decides
  // whether to show/hide the banner state machine.
  function onFirstLedgerForThisSocket() {
    clearWatchdog();
    if (isOnPrimary()) {
      // Primary is delivering — hide any banner, stop the primary-retry
      // background loop.
      setBanner('hidden');
      clearPrimaryRetry();
    } else {
      // We're on a fallback URL and it's live — now (and only now)
      // is the "fallback banner" state genuinely correct.
      noteFallbackLive('fallback:first-ledger-on-non-primary');
    }
  }

  function connect() {
    if (closed) return;
    if (typeof WebSocket === 'undefined') return;

    var url = currentUrl();
    // First-connect UX: if PRIMARY_URL is configured and we're aiming at
    // it, immediately show the "Connecting to our own node…" state so the
    // reader sees the primary intent, not a jarring "fallback" banner.
    if (PRIMARY_URL && url === PRIMARY_URL && bannerState === 'hidden'
        && !lastLedger) {
      setBanner('connecting');
    }
    try { ws = new WebSocket(url); }
    catch (e) { scheduleReconnect(); return; }

    // Per-socket flag so onmessage's first-ledger hook only fires once
    // per WebSocket lifetime (not for every ledgerClosed after the first).
    ws.__gotFirstLedger = false;

    ws.onopen = function () {
      reconnectDelay = 1500;
      connected = true;
      fireStatus('open');
      try {
        ws.send(JSON.stringify({
          id: 'live-stream',
          command: 'subscribe',
          streams: ['ledger']
        }));
      } catch (e) {}
      // Primary-only watchdog: subscribe response usually carries an
      // immediate ledger, so no ledger within PRIMARY_LEDGER_WATCHDOG_MS
      // means the primary is half-open (TCP up, no data) — close and
      // step to next. Do NOT fire fallback banner or telemetry here —
      // fallback state fires only on FIRST-LEDGER-on-non-primary.
      if (isOnPrimary()) {
        clearWatchdog();
        primaryWatchdogTimer = setTimeout(function () {
          primaryWatchdogTimer = null;
          if (lastLedger && lastLedgerReceivedAt &&
              Date.now() - lastLedgerReceivedAt < PRIMARY_LEDGER_WATCHDOG_MS) {
            return; // got a ledger in time
          }
          // Silent close + step to next URL. Banner stays on 'connecting'
          // until either the next URL delivers a ledger (→ 'fallback') or
          // primary reconnects successfully later (→ 'hidden').
          urlIdx++;
          try { ws.close(); } catch (e) {}
        }, PRIMARY_LEDGER_WATCHDOG_MS);
      }
    };

    ws.onmessage = function (msg) {
      var data;
      try { data = JSON.parse(msg.data); } catch (e) { return; }
      if (!data) return;
      // Subscribe response carries the current validated ledger — fire
      // immediately so the first paint isn't blank for 3.5s.
      if (data.type === 'response' && data.result && data.result.ledger_index) {
        if (!ws.__gotFirstLedger) {
          ws.__gotFirstLedger = true;
          onFirstLedgerForThisSocket();
        }
        fireLedger({
          type: 'ledgerClosed',
          ledger_index: data.result.ledger_index,
          ledger_hash: data.result.ledger_hash,
          ledger_time: data.result.ledger_time,
          fee_base: data.result.fee_base,
          reserve_base: data.result.reserve_base,
          reserve_inc: data.result.reserve_inc,
          _seed: true
        });
        return;
      }
      if (data.type === 'ledgerClosed') {
        if (!ws.__gotFirstLedger) {
          ws.__gotFirstLedger = true;
          onFirstLedgerForThisSocket();
        }
        fireLedger(data);
      }
    };

    ws.onerror = function () {
      // Do NOT ping fallback telemetry or flip banner here — onerror
      // is followed by onclose which handles the step. Firing on
      // onerror while still on the primary URL was the source of the
      // primary→primary false-positive telemetry rows.
    };

    ws.onclose = function () {
      clearWatchdog();
      connected = false;
      fireStatus('closed');
      if (closed) return;
      // Step to the next URL in the WS_URLS list. If we started on
      // primary and it failed, urlIdx++ moves to the fallback list;
      // if we're already downstream, keep stepping.
      urlIdx++;
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 1.5, MAX_RECONNECT_DELAY);
  }

  window.addEventListener('beforeunload', function () {
    closed = true;
    clearWatchdog();
    clearPrimaryRetry();
    if (ws) { try { ws.close(); } catch (e) {} }
  });

  window.LiveStream = {
    /* Subscribe to ledgerClosed events. Returns an unsubscribe function.
     * If a ledger has already been seen, the callback fires once
     * synchronously with the most recent event so late subscribers
     * still get the current state. */
    onLedger: function (cb) {
      if (typeof cb !== 'function') return function () {};
      ledgerListeners.push(cb);
      if (lastLedger) { try { cb(lastLedger); } catch (e) {} }
      return function () {
        var i = ledgerListeners.indexOf(cb);
        if (i >= 0) ledgerListeners.splice(i, 1);
      };
    },
    /* Subscribe to socket status events ('open' | 'closed'). */
    onStatus: function (cb) {
      if (typeof cb !== 'function') return function () {};
      statusListeners.push(cb);
      try { cb(connected ? 'open' : 'closed'); } catch (e) {}
      return function () {
        var i = statusListeners.indexOf(cb);
        if (i >= 0) statusListeners.splice(i, 1);
      };
    },
    isConnected: function () { return connected; },
    getCurrentUrl: function () { return currentUrl(); },
    isOnPrimary: function () { return isOnPrimary(); },
    getLastLedger: function () { return lastLedger; },
    getLastLedgerReceivedAt: function () { return lastLedgerReceivedAt; }
  };

  connect();
})(window);
