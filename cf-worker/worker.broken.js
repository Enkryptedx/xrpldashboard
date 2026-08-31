// FAIL-OPEN TEST — deliberately broken read on request.cf.
//
// Purpose: prove the try/catch fail-open guarantee actually catches. We
// deploy THIS version first, hit the site through it, and confirm the site
// still loads normally (i.e. the try/catch swallows the deliberate error
// and passes the original request through unmodified). Only after that
// passes do we deploy worker.js (the real version).
//
// The break: `request.cf.thisFieldDoesNotExist.subfield.chain` — accessing
// a subfield of undefined guarantees a TypeError at runtime. If the outer
// site loads normally with this deployed, fail-open is verified.

export default {
  async fetch(request) {
    try {
      let outbound = request;
      try {
        const bogus = request.cf.thisFieldDoesNotExist.subfield.chain;
        const headers = new Headers(request.headers);
        headers.set('X-CF-Region-Code', bogus);
        outbound = new Request(request, { headers });
      } catch (e) {
        outbound = request;
      }
      return await fetch(outbound);
    } catch (e) {
      return await fetch(request);
    }
  }
};
