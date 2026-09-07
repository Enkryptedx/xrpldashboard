#!/usr/bin/env python3
"""All-time country map + 50-state progress (read-only). 2026-09-03."""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone

HERE = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, HERE)
import db  # noqa: E402

STATE_CLOCK_EPOCH = 1788295800  # 2026-09-01 20:50:00 UTC

# ISO 3166-1 alpha-2 → country name (sourced from ISO; kept complete for any
# code that could appear in page_views.country). "T1" is Tor exit placeholder.
COUNTRY_NAMES = {
    "AD":"Andorra","AE":"United Arab Emirates","AF":"Afghanistan","AG":"Antigua and Barbuda",
    "AI":"Anguilla","AL":"Albania","AM":"Armenia","AO":"Angola","AQ":"Antarctica",
    "AR":"Argentina","AS":"American Samoa","AT":"Austria","AU":"Australia","AW":"Aruba",
    "AX":"Åland Islands","AZ":"Azerbaijan","BA":"Bosnia and Herzegovina",
    "BB":"Barbados","BD":"Bangladesh","BE":"Belgium","BF":"Burkina Faso","BG":"Bulgaria",
    "BH":"Bahrain","BI":"Burundi","BJ":"Benin","BL":"Saint Barthélemy","BM":"Bermuda",
    "BN":"Brunei","BO":"Bolivia","BQ":"Bonaire, Sint Eustatius and Saba","BR":"Brazil",
    "BS":"Bahamas","BT":"Bhutan","BV":"Bouvet Island","BW":"Botswana","BY":"Belarus",
    "BZ":"Belize","CA":"Canada","CC":"Cocos (Keeling) Islands","CD":"DR Congo",
    "CF":"Central African Republic","CG":"Congo","CH":"Switzerland","CI":"Côte d'Ivoire",
    "CK":"Cook Islands","CL":"Chile","CM":"Cameroon","CN":"China","CO":"Colombia",
    "CR":"Costa Rica","CU":"Cuba","CV":"Cape Verde","CW":"Curaçao","CX":"Christmas Island",
    "CY":"Cyprus","CZ":"Czechia","DE":"Germany","DJ":"Djibouti","DK":"Denmark",
    "DM":"Dominica","DO":"Dominican Republic","DZ":"Algeria","EC":"Ecuador","EE":"Estonia",
    "EG":"Egypt","EH":"Western Sahara","ER":"Eritrea","ES":"Spain","ET":"Ethiopia",
    "FI":"Finland","FJ":"Fiji","FK":"Falkland Islands","FM":"Micronesia","FO":"Faroe Islands",
    "FR":"France","GA":"Gabon","GB":"United Kingdom","GD":"Grenada","GE":"Georgia",
    "GF":"French Guiana","GG":"Guernsey","GH":"Ghana","GI":"Gibraltar","GL":"Greenland",
    "GM":"Gambia","GN":"Guinea","GP":"Guadeloupe","GQ":"Equatorial Guinea","GR":"Greece",
    "GS":"South Georgia","GT":"Guatemala","GU":"Guam","GW":"Guinea-Bissau","GY":"Guyana",
    "HK":"Hong Kong","HM":"Heard and McDonald Islands","HN":"Honduras","HR":"Croatia",
    "HT":"Haiti","HU":"Hungary","ID":"Indonesia","IE":"Ireland","IL":"Israel","IM":"Isle of Man",
    "IN":"India","IO":"British Indian Ocean Territory","IQ":"Iraq","IR":"Iran","IS":"Iceland",
    "IT":"Italy","JE":"Jersey","JM":"Jamaica","JO":"Jordan","JP":"Japan","KE":"Kenya",
    "KG":"Kyrgyzstan","KH":"Cambodia","KI":"Kiribati","KM":"Comoros","KN":"Saint Kitts and Nevis",
    "KP":"North Korea","KR":"South Korea","KW":"Kuwait","KY":"Cayman Islands","KZ":"Kazakhstan",
    "LA":"Laos","LB":"Lebanon","LC":"Saint Lucia","LI":"Liechtenstein","LK":"Sri Lanka",
    "LR":"Liberia","LS":"Lesotho","LT":"Lithuania","LU":"Luxembourg","LV":"Latvia",
    "LY":"Libya","MA":"Morocco","MC":"Monaco","MD":"Moldova","ME":"Montenegro",
    "MF":"Saint Martin","MG":"Madagascar","MH":"Marshall Islands","MK":"North Macedonia",
    "ML":"Mali","MM":"Myanmar","MN":"Mongolia","MO":"Macau","MP":"Northern Mariana Islands",
    "MQ":"Martinique","MR":"Mauritania","MS":"Montserrat","MT":"Malta","MU":"Mauritius",
    "MV":"Maldives","MW":"Malawi","MX":"Mexico","MY":"Malaysia","MZ":"Mozambique",
    "NA":"Namibia","NC":"New Caledonia","NE":"Niger","NF":"Norfolk Island","NG":"Nigeria",
    "NI":"Nicaragua","NL":"Netherlands","NO":"Norway","NP":"Nepal","NR":"Nauru","NU":"Niue",
    "NZ":"New Zealand","OM":"Oman","PA":"Panama","PE":"Peru","PF":"French Polynesia",
    "PG":"Papua New Guinea","PH":"Philippines","PK":"Pakistan","PL":"Poland",
    "PM":"Saint Pierre and Miquelon","PN":"Pitcairn Islands","PR":"Puerto Rico","PS":"Palestine",
    "PT":"Portugal","PW":"Palau","PY":"Paraguay","QA":"Qatar","RE":"Réunion","RO":"Romania",
    "RS":"Serbia","RU":"Russia","RW":"Rwanda","SA":"Saudi Arabia","SB":"Solomon Islands",
    "SC":"Seychelles","SD":"Sudan","SE":"Sweden","SG":"Singapore","SH":"Saint Helena",
    "SI":"Slovenia","SJ":"Svalbard and Jan Mayen","SK":"Slovakia","SL":"Sierra Leone",
    "SM":"San Marino","SN":"Senegal","SO":"Somalia","SR":"Suriname","SS":"South Sudan",
    "ST":"São Tomé and Príncipe","SV":"El Salvador","SX":"Sint Maarten","SY":"Syria",
    "SZ":"Eswatini","TC":"Turks and Caicos","TD":"Chad","TF":"French Southern Territories",
    "TG":"Togo","TH":"Thailand","TJ":"Tajikistan","TK":"Tokelau","TL":"Timor-Leste",
    "TM":"Turkmenistan","TN":"Tunisia","TO":"Tonga","TR":"Turkey","TT":"Trinidad and Tobago",
    "TV":"Tuvalu","TW":"Taiwan","TZ":"Tanzania","UA":"Ukraine","UG":"Uganda",
    "UM":"US Minor Outlying Islands","US":"United States","UY":"Uruguay","UZ":"Uzbekistan",
    "VA":"Vatican City","VC":"Saint Vincent and the Grenadines","VE":"Venezuela",
    "VG":"British Virgin Islands","VI":"US Virgin Islands","VN":"Vietnam","VU":"Vanuatu",
    "WF":"Wallis and Futuna","WS":"Samoa","YE":"Yemen","YT":"Mayotte","ZA":"South Africa",
    "ZM":"Zambia","ZW":"Zimbabwe","XK":"Kosovo","T1":"Tor exit (placeholder)",
}

# All 50 US states.
STATES_50 = {
    "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California",
    "CO":"Colorado","CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia",
    "HI":"Hawaii","ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa","KS":"Kansas",
    "KY":"Kentucky","LA":"Louisiana","ME":"Maine","MD":"Maryland","MA":"Massachusetts",
    "MI":"Michigan","MN":"Minnesota","MS":"Mississippi","MO":"Missouri","MT":"Montana",
    "NE":"Nebraska","NV":"Nevada","NH":"New Hampshire","NJ":"New Jersey","NM":"New Mexico",
    "NY":"New York","NC":"North Carolina","ND":"North Dakota","OH":"Ohio","OK":"Oklahoma",
    "OR":"Oregon","PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina",
    "SD":"South Dakota","TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont",
    "VA":"Virginia","WA":"Washington","WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming",
}
# Non-state territories reported separately.
US_TERRITORIES = {
    "DC":"District of Columbia","PR":"Puerto Rico","VI":"US Virgin Islands",
    "GU":"Guam","AS":"American Samoa","MP":"Northern Mariana Islands",
}


def run():
    with db.pg_connect() as conn:
        c = conn.cursor()

        print("=" * 84)
        print("FULL COUNTRY MAP — all-time (excluding T1), first-seen order")
        print("=" * 84)
        # First-seen per country + whether any-human record exists
        c.execute("""
            SELECT country,
                   MIN(ts) AS first_ts,
                   MAX(CASE WHEN is_bot IS NOT TRUE THEN 1 ELSE 0 END) AS has_human
            FROM page_views
            WHERE country IS NOT NULL AND country <> 'T1'
            GROUP BY country
            ORDER BY first_ts, country
        """)
        rows = c.fetchall()
        print(f"{'code':4s} {'name':32s} {'first_seen':10s} {'source':10s}")
        print("-" * 84)
        human_count = 0
        bot_only_count = 0
        for co, ts, has_human in rows:
            name = COUNTRY_NAMES.get(co, "(unknown)")
            d = datetime.fromtimestamp(ts, timezone.utc).date()
            src = "human" if has_human else "bot-only"
            if has_human:
                human_count += 1
            else:
                bot_only_count += 1
            print(f"{co:4s} {name:32s} {str(d):10s} {src:10s}")

        print("-" * 84)
        print(f"Totals: {len(rows)} countries (excluding T1) — "
              f"{human_count} with human visitors, {bot_only_count} bot-only so far")

        print()
        print("=" * 84)
        print("50-STATE PROGRESS — since 2026-09-01 20:50 UTC")
        print("=" * 84)

        # Human-seen states
        c.execute("""
            SELECT substr(region_code, 4) AS code, MIN(ts) AS first_ts
            FROM page_views
            WHERE region_code LIKE 'US-%%' AND is_bot IS NOT TRUE
              AND ts >= %s
            GROUP BY code
            ORDER BY first_ts
        """, (STATE_CLOCK_EPOCH,))
        human_seen_pairs = c.fetchall()
        human_seen_codes = set()

        print("\n--- Human-visitor states (first-seen order) ---")
        for code, ts in human_seen_pairs:
            if code in STATES_50:
                human_seen_codes.add(code)
                d = datetime.fromtimestamp(ts, timezone.utc)
                print(f"  {d.date()} {d.strftime('%H:%M UTC')}  {code}  {STATES_50[code]}")

        # Any-traffic states
        c.execute("""
            SELECT substr(region_code, 4) AS code, MIN(ts) AS first_ts
            FROM page_views
            WHERE region_code LIKE 'US-%%' AND ts >= %s
            GROUP BY code
            ORDER BY first_ts
        """, (STATE_CLOCK_EPOCH,))
        any_seen_pairs = c.fetchall()
        any_seen_codes = set()
        for code, _ in any_seen_pairs:
            if code in STATES_50:
                any_seen_codes.add(code)

        # Bot-only states = any \ human
        bot_only_state_codes = any_seen_codes - human_seen_codes
        # First-seen for bot-only states from any_seen_pairs
        print("\n--- Bot-only states (first-seen order) ---")
        for code, ts in any_seen_pairs:
            if code in bot_only_state_codes:
                d = datetime.fromtimestamp(ts, timezone.utc)
                print(f"  {d.date()} {d.strftime('%H:%M UTC')}  {code}  {STATES_50[code]}")
        if not bot_only_state_codes:
            print("  (none)")

        # Not-yet-seen states
        not_seen = set(STATES_50) - any_seen_codes
        print(f"\n--- NOT YET SEEN AT ALL ({len(not_seen)} to go) ---")
        for code in sorted(not_seen):
            print(f"  {code}  {STATES_50[code]}")

        print("\n--- Counts ---")
        print(f"  {len(human_seen_codes)} of 50 from humans")
        print(f"  {len(any_seen_codes)} of 50 from anyone")
        print(f"  {len(not_seen)} to go")

        # Territories seen (not counted toward 50)
        c.execute("""
            SELECT substr(region_code, 4) AS code,
                   MIN(ts) AS first_ts,
                   MAX(CASE WHEN is_bot IS NOT TRUE THEN 1 ELSE 0 END) AS has_human
            FROM page_views
            WHERE region_code LIKE 'US-%%' AND ts >= %s
              AND substr(region_code, 4) IN %s
            GROUP BY code
            ORDER BY first_ts
        """, (STATE_CLOCK_EPOCH, tuple(US_TERRITORIES.keys())))
        terr = c.fetchall()
        print("\n--- Territories seen (reported separately, not counted toward 50) ---")
        if terr:
            for code, ts, has_h in terr:
                d = datetime.fromtimestamp(ts, timezone.utc)
                src = "human" if has_h else "bot-only"
                print(f"  {d.date()} {d.strftime('%H:%M UTC')}  {code}  {US_TERRITORIES[code]}  {src}")
        else:
            print("  (none yet)")


if __name__ == "__main__":
    run()
