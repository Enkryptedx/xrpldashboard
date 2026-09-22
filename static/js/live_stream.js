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

  // ~10s watchdog: if the socket opens on the primary but no ledgerClosed
  // arrives by this deadline, treat as failed and step to the next URL.
  var PRIMARY_LEDGER_WATCHDOG_MS = 10000;

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
  var fallbackPinged = false;
  var bannerEl = null;

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

  function showFallbackBanner() {
    if (typeof document === 'undefined') return;
    if (bannerEl) return;
    var el = document.createElement('div');
    el.setAttribute('role', 'status');
    el.setAttribute('data-live-stream-fallback-banner', '1');
    el.style.cssText = [
      'position:fixed', 'left:12px', 'bottom:12px', 'z-index:2147483000',
      'padding:8px 12px', 'border-radius:6px',
      'background:#ffb020', 'color:#1a1400',
      'font:12px/1.4 system-ui,-apple-system,Segoe UI,sans-serif',
      'box-shadow:0 2px 8px rgba(0,0,0,0.15)',
      'max-width:320px', 'cursor:default'
    ].join(';');
    el.textContent = 'Live stream: using ' + FALLBACK_LABEL +
      ' — own-node stream (' + (PRIMARY_LABEL || 'primary') +
      ') temporarily unreachable';
    if (document.body) {
      document.body.appendChild(el);
      bannerEl = el;
    } else {
      document.addEventListener('DOMContentLoaded', function () {
        if (!bannerEl && document.body) {
          document.body.appendChild(el);
          bannerEl = el;
        }
      });
    }
  }

  function hideFallbackBanner() {
    if (bannerEl && bannerEl.parentNode) {
      bannerEl.parentNode.removeChild(bannerEl);
    }
    bannerEl = null;
  }

  function pingFallbackTelemetry(reason) {
    if (fallbackPinged) return;
    fallbackPinged = true;
    if (!PRIMARY_URL) return;
    try {
      var params = new URLSearchParams({
        source: 'browser_wss',
        primary: PRIMARY_URL,
        fallback: currentUrl(),
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

  function noteFallbackReached(reason) {
    showFallbackBanner();
    pingFallbackTelemetry(reason);
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

  function connect() {
    if (closed) return;
    if (typeof WebSocket === 'undefined') return;

    var url = currentUrl();
    try { ws = new WebSocket(url); }
    catch (e) { scheduleReconnect(); return; }

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
      // immediate ledger, so no ledger within 10s means the primary is
      // half-open (TCP up, no data) — close and step to next.
      if (isOnPrimary()) {
        clearWatchdog();
        primaryWatchdogTimer = setTimeout(function () {
          primaryWatchdogTimer = null;
          if (lastLedger && lastLedgerReceivedAt &&
              Date.now() - lastLedgerReceivedAt < PRIMARY_LEDGER_WATCHDOG_MS) {
            return; // got a ledger in time
          }
          noteFallbackReached('watchdog:no-ledger');
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
        clearWatchdog();
        if (isOnPrimary()) hideFallbackBanner();
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
        clearWatchdog();
        if (isOnPrimary()) hideFallbackBanner();
        fireLedger(data);
      }
    };

    ws.onerror = function () {
      if (isOnPrimary()) noteFallbackReached('onerror');
    };

    ws.onclose = function () {
      clearWatchdog();
      connected = false;
      fireStatus('closed');
      if (closed) return;
      // If we were on the primary and never got a ledger, we already
      // pinged; step off. If we were downstream, keep stepping.
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
