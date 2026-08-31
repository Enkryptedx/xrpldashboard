// Cloudflare Worker: xrpldashboard-region-enrich
// Route: xrpldashboard.com/* + www.xrpldashboard.com/*
//
// Purpose:
// Read request.cf.regionCode + request.cf.region (populated on Free-tier
// Workers per CF docs — different data path than the "Add visitor location
// headers" Managed Transform, which requires a paid IP-geolocation dataset
// for region-level values). Inject those as X-CF-Region-Code + X-CF-Region
// on the outbound request to origin so Flask _log_page_view can capture
// state-level analytics into page_views.region_code. Existing CF-IPCountry
// header is untouched — the Flask writer already reads that separately.
//
// FAIL-OPEN GUARANTEE (load-bearing):
// This Worker sits on the request path for EVERY visitor to xrpldashboard.com.
// Under ANY exception in the enrichment path, we must pass the request through
// unmodified rather than 522/1101 the user. Two nested try/catches enforce
// this:
//   - inner: enrichment failure -> outbound reverts to original request
//   - outer: fetch() failure with modified request -> retry with original
// The site's uptime takes strict precedence over analytics completeness.

export default {
  async fetch(request) {
    try {
      let outbound = request;
      try {
        const cf = request.cf;
        if (cf) {
          const regionCode = cf.regionCode;
          const region = cf.region;
          if (regionCode || region) {
            const headers = new Headers(request.headers);
            if (regionCode) headers.set('X-CF-Region-Code', regionCode);
            if (region) headers.set('X-CF-Region', region);
            outbound = new Request(request, { headers });
          }
        }
      } catch (e) {
        outbound = request;
      }
      return await fetch(outbound);
    } catch (e) {
      return await fetch(request);
    }
  }
};
