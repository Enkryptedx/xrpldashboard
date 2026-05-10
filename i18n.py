"""Internationalization for xrpldashboard.

Why a separate module?
----------------------
Flask-Babel needs (1) an init call against the app, (2) a locale selector
that decides which language to render, and (3) helpers used by templates
(language list, current locale, switcher URL). Putting all that here
keeps `app.py` free of i18n plumbing — it just imports `init_i18n`.

Locale strategy (Phase 1):
  • Cookie `xrpl_lang=<code>` — set by /lang/<code>, lasts 1 year
  • Falls back to Accept-Language header
  • Final fallback: English

Path-prefix routing (`/es/pools`) is deferred to Phase 2 once we've
proven the translation pipeline works. Cookie-based switches the UI
language without touching every URL in the app, which means we can
ship Spanish today rather than refactoring 60+ routes first.

Crypto glossary policy:
  • Proper-noun-of-crypto terms (XRP, XRPL, AMM, ledger, trustline,
    RLUSD, gateway) stay in English even in translated locales.
    Translating them confuses crypto-literate users in every market.
  • Numbers, addresses, currency codes are never wrapped in _().
"""

from __future__ import annotations

from flask import request, make_response, redirect, url_for, abort
from flask_babel import Babel, gettext as _, lazy_gettext as _l


# Language list. Order = display order in the switcher dropdown.
# Each entry: (code, native_label, english_label, [optional: dir])
# Native labels are what speakers actually call their own language —
# users should be able to recognize their language without knowing English.
LANGUAGES = [
    ("en", "English",      "English"),
    ("es", "Español",      "Spanish"),
    ("zh", "中文",          "Chinese (Simplified)"),
    ("ja", "日本語",         "Japanese"),
    ("ko", "한국어",         "Korean"),
    ("pt", "Português",    "Portuguese (Brazil)"),
    ("hi", "हिन्दी",         "Hindi"),
    ("ar", "العربية",       "Arabic", "rtl"),
    ("fr", "Français",     "French"),
    ("de", "Deutsch",      "German"),
]

LANGUAGE_CODES = [c for (c, *_rest) in LANGUAGES]
RTL_LANGUAGES = {entry[0] for entry in LANGUAGES if len(entry) > 3 and entry[3] == "rtl"}
COOKIE_NAME = "xrpl_lang"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


def select_locale() -> str:
    """Resolve the locale for the current request.

    Order: explicit cookie → Accept-Language header → English. Cookie wins
    so a user's manual switch sticks across pages. We restrict to the
    LANGUAGE_CODES whitelist to prevent open-ended locale strings from
    breaking template rendering.
    """
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie in LANGUAGE_CODES:
        return cookie
    best = request.accept_languages.best_match(LANGUAGE_CODES)
    return best or "en"


def init_i18n(app):
    """Wire Flask-Babel into `app`. Registers:
      • Babel with the locale selector
      • /lang/<code> route that flips the cookie and bounces back
      • Jinja globals: `_`, `language_list`, `current_locale`, `is_rtl`
    """
    babel = Babel()
    babel.init_app(app, locale_selector=select_locale, default_translation_directories="translations")

    @app.route("/lang/<code>")
    def set_language(code):
        """Switch language. Sets the cookie and redirects back to the
        referrer (or homepage). Whitelisted to known locales so this
        endpoint can't be abused to set arbitrary cookie values."""
        if code not in LANGUAGE_CODES:
            abort(404)
        target = request.referrer or url_for("index")
        resp = make_response(redirect(target))
        resp.set_cookie(
            COOKIE_NAME, code,
            max_age=COOKIE_MAX_AGE,
            httponly=False,  # JS can read it for client-side hints
            samesite="Lax",
        )
        return resp

    @app.context_processor
    def _i18n_globals():
        """Expose helpers every template needs for the language switcher
        and for direction-aware styling. Cheap to compute per request."""
        from flask_babel import get_locale
        loc = str(get_locale() or "en")
        return {
            "language_list": LANGUAGES,
            "current_locale": loc,
            "is_rtl": loc in RTL_LANGUAGES,
        }

    return babel
