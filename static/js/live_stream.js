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
 * wss://s2.ripple.com. Going through Flask would tie up gunicorn workers
 * for every connected tab on the Render free tier. This is cheaper and
 * lower-latency for the panels that just need "did a ledger just close."
 *
 * Anything that needs server-derived data (load_factor, XRP/USD oracle,
 * Postgres-backed counters) still polls the JSON API on a slower cadence.
 */
(function (window) {
  'use strict';
  if (window.LiveStream) return;

  // Primary + fallbacks. CSP allows all three (see _CSP_CONNECT_SRC in app.py).
  // We only step to a fallback after the primary refuses to connect, then
  // step back to primary on the next successful open.
  var WS_URLS = [
    'wss://s2.ripple.com',
    'wss://xrplcluster.com',
    'wss://s1.ripple.com'
  ];

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

    var url = WS_URLS[urlIdx % WS_URLS.length];
    try { ws = new WebSocket(url); }
    catch (e) { scheduleReconnect(); return; }

    ws.onopen = function () {
      reconnectDelay = 1500;
      urlIdx = 0;
      connected = true;
      fireStatus('open');
      try {
        ws.send(JSON.stringify({
          id: 'live-stream',
          command: 'subscribe',
          streams: ['ledger']
        }));
      } catch (e) {}
    };

    ws.onmessage = function (msg) {
      var data;
      try { data = JSON.parse(msg.data); } catch (e) { return; }
      if (!data) return;
      // Subscribe response carries the current validated ledger — fire
      // immediately so the first paint isn't blank for 3.5s.
      if (data.type === 'response' && data.result && data.result.ledger_index) {
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
      if (data.type === 'ledgerClosed') fireLedger(data);
    };

    ws.onerror = function () {};

    ws.onclose = function () {
      connected = false;
      fireStatus('closed');
      if (closed) return;
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
    getLastLedger: function () { return lastLedger; },
    getLastLedgerReceivedAt: function () { return lastLedgerReceivedAt; }
  };

  connect();
})(window);
