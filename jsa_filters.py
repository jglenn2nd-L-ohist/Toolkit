#!/usr/bin/env python3
"""
jsa_filters.py — the ONE place JSA eligibility rules live.

Both scrapers and the backfill import this module. Change a rule here,
commit, and every stage picks it up on its next run.

Design principle: FAIL CLOSED.
  pass   -> job is proven eligible            -> status 'New'
  review -> eligibility can't be proven       -> status 'Review'
  reject -> job is proven ineligible          -> jsa_rejects.json (with reason)

evaluate() never silently drops anything. Every decision carries reason codes.
"""

import re

FILTER_VERSION = '2026-09-24.4'   # bump when rules change; stamped on every record

# ── WHERE YOU CAN WORK ────────────────────────────────────
ALLOWED_STATES = {'GA', 'FL', 'NC', 'SC'}
# Texas is allowed only for these metros (edit freely)
ALLOWED_TX_CITIES = {
    # Dallas-Fort Worth metro
    'dallas', 'fort worth', 'irving', 'plano', 'frisco', 'richardson', 'addison', 'arlington',
    'lewisville', 'farmers branch', 'carrollton', 'burleson', 'mansfield', 'red oak',
    'lancaster', 'denton', 'mckinney', 'allen', 'garland', 'mesquite', 'grand prairie',
    'coppell', 'grapevine', 'southlake', 'rockwall', 'flower mound', 'cedar hill', 'desoto',
    'duncanville', 'euless', 'bedford', 'hurst', 'keller', 'the colony', 'little elm',
    'prosper', 'westlake', 'wylie', 'rowlett', 'dfw',
    # Houston metro
    'houston', 'sugar land', 'katy', 'the woodlands', 'pasadena', 'spring', 'humble',
    'cypress', 'tomball', 'conroe', 'pearland', 'league city', 'baytown', 'missouri city',
    'stafford', 'kingwood', 'friendswood', 'bellaire', 'webster', 'richmond', 'rosenberg',
}
# Metro-area labels LinkedIn uses with no state ('Greater Chicago Area')
METRO_STATES = {
    'chicago': 'IL', 'baton rouge': 'LA', 'new orleans': 'LA', 'denver': 'CO',
    'new york': 'NY', 'boston': 'MA', 'seattle': 'WA', 'phoenix': 'AZ', 'san francisco': 'CA',
    'los angeles': 'CA', 'bay area': 'CA', 'philadelphia': 'PA', 'pittsburgh': 'PA',
    'detroit': 'MI', 'minneapolis': 'MN', 'st. louis': 'MO', 'kansas city': 'MO',
    'nashville': 'TN', 'austin': 'TX', 'san antonio': 'TX', 'salt lake': 'UT',
    'las vegas': 'NV', 'portland': 'OR', 'washington dc': 'DC', 'dc-baltimore': 'DC',
    'columbus': 'OH', 'cleveland': 'OH', 'cincinnati': 'OH', 'indianapolis': 'IN',
    'milwaukee': 'WI', 'richmond, va': 'VA', 'birmingham': 'AL',
}

STATE_NAMES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR', 'california': 'CA',
    'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE', 'district of columbia': 'DC',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID', 'illinois': 'IL',
    'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA',
    'maine': 'ME', 'maryland': 'MD', 'massachusetts': 'MA', 'michigan': 'MI',
    'minnesota': 'MN', 'mississippi': 'MS', 'missouri': 'MO', 'montana': 'MT',
    'nebraska': 'NE', 'nevada': 'NV', 'new hampshire': 'NH', 'new jersey': 'NJ',
    'new mexico': 'NM', 'new york': 'NY', 'north carolina': 'NC', 'north dakota': 'ND',
    'ohio': 'OH', 'oklahoma': 'OK', 'oregon': 'OR', 'pennsylvania': 'PA',
    'rhode island': 'RI', 'south carolina': 'SC', 'south dakota': 'SD', 'tennessee': 'TN',
    'texas': 'TX', 'utah': 'UT', 'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA',
    'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
}
STATE_CODES = set(STATE_NAMES.values())

FOREIGN = [
    'canada', 'united kingdom', ' uk', 'england', 'australia', 'india', 'philippines',
    'egypt', 'vietnam', 'france', 'singapore', 'new zealand', 'mexico', 'brazil',
    'germany', 'ireland', 'poland', 'romania', 'colombia', 'argentina', 'pakistan',
    'toronto', 'ontario', 'vancouver', 'london', 'bangalore', 'bengaluru', 'hyderabad',
    'manila', 'cairo', 'emea', 'apac', 'latam',
]

US_REMOTE_MARKERS = ['united states', 'remote', 'usa', 'u.s.', 'nationwide', 'anywhere in the us']

# ── WHAT YOU'RE TARGETING ─────────────────────────────────
# Your 11 stated titles (+ 'bi analyst'). The old email scraper had drifted to ~50 patterns
# (tax, actuarial, credit, audit...). Add back anything you actually want.
TARGET_TITLES = [
    'data analyst', 'business analyst', 'operations analyst', 'business intelligence',
    'bi analyst', 'marketing analyst', 'strategy analyst', 'analytics engineer',
    'reporting analyst', 'product analyst', 'systems analyst', 'supply chain analyst',
    # added 09-24 after reject audit — families you have actually applied to
    'analytics specialist', 'data analytics', 'insights', 'data visualization',
    'logistics analyst', 'sales operations', 'revenue operations', 'analytics associate',
    'operations data', 'marketing analytics',
]
# Titles containing these words but no TARGET_TITLES match go to REVIEW, not reject.
# Your call on each; promote recurring families into TARGET_TITLES.
ANALYST_WORDS = r'\b(analyst|analytics|insights?|intelligence)\b'

# Word-boundary regexes so 'staff' doesn't hit 'staffing', 'lead' doesn't hit 'leader'
TITLE_EXCLUDE = [
    r'\bsenior\b', r'\bsr\b\.?', r'\blead\b', r'\bprincipal\b', r'\bstaff\b',
    r'\bintern(ship)?\b', r'\bco-?op\b', r'\bdirector\b', r'\bvp\b', r'\bvice president\b',
    r'\bhead of\b', r'\bmanager\b', r'\bsummer 20\d\d\b',
    r'cyber ?security', r'information security', r'\binfosec\b', r'network security',
    r'\bsoc analyst\b', r'threat intel', r'penetration test',
    r'\bcollections\b', r'accounts (payable|receivable)', r'\bcard dispute', r'\bchargeback',
    r'software engineer', r'\bdevops\b', r'cloud engineer', r'full[- ]?stack',
    r'\bwarehouse\b', r'\bpurchasing\b', r'\badministrative\b',
    r'demand\b.{0,15}plann', r'(supply|material|inventory|demand) plann', r'\bforecast', r'salesforce admin', r'\bfp&a\b', r'\bcapex\b',
    r'\b(java|c\+\+|\.net) developer\b', r'\bskillbridge\b',
]

JUNK_TITLE = [r'^your job alert for', r'^jobs? (similar|for you)', r'^see all', r'^view ']

# ── JD BODY RULES ─────────────────────────────────────────
JD_REJECT = {
    'clearance_required': r'(security clearance|active (secret|top secret|ts)|ts/sci|'
                          r'secret clearance|public trust|clearance (is )?required|'
                          r'must (be able to )?(obtain|hold|possess) (a|an) [a-z ]{0,20}clearance)',
    'masters_required':   r"(master'?s degree (is )?required|requires? an? master'?s|"
                          r"master'?s degree in [^.]{0,60}\b(is )?required)",
}
# Body terms from your exclusion list. These go to REVIEW, not reject: phrases like
# 'user stories' appear in plenty of normal analyst JDs, and silent keyword kills are
# how good roles vanish without you ever seeing them.
JD_REVIEW = {
    'jd_term:sdlc/wireframes/user_stories': r'\b(sdlc|wireframes?|user stories)\b',
    'jd_term:erp_implementation':           r'(erp implementation|\bepicor\b|\banaplan\b)',
    'jd_term:onsite_or_hybrid':             r'\b(hybrid|on-?site|in[- ]office)\b',
}
RESIDENCY_TRIGGERS = (
    r'(must (reside|live|be located|be based)|residents? of|reside in|located in one of|'
    r'eligible (states|locations)|able to work from|open to candidates (in|located)|'
    r'(only|currently) (hiring|accepting) (in|from)|not able to (hire|consider) (in|candidates))'
)

# LinkedIn 'Remote (US)' searches return postings whose location is a specific city
# (e.g. 'Columbia, MD'). Some are remote-anywhere with an HQ label; most in practice are
# state-restricted or hybrid. 'reject' keeps your feed clean (rejects file is the audit trail);
# 'review' surfaces them for your call.
REMOTE_OUT_OF_AREA_POLICY = 'reject'

SALARY_FLOOR = 60000   # annual; only applied when a salary is actually posted

COMPANY_BLOCK = []   # lowercase substrings; empty on purpose — add only after evidence


# ── LOCATION PARSING ──────────────────────────────────────
def _states_in(text):
    """All US state codes mentioned in text (names or ', XX' / ' XX ' codes)."""
    t = ' ' + text.lower() + ' '
    found = {code for name, code in STATE_NAMES.items()
             if re.search(r'\b' + name + r'\b(?! city)', t)}
    # 'washington' alone is ambiguous with DC; ignore if 'washington dc/d.c.' present
    if re.search(r'washington,?\s*d\.?c', t):
        found.discard('WA'); found.add('DC')
    for m in re.finditer(r'(?:,|\()\s*([A-Z]{2})\b', text):
        if m.group(1) in STATE_CODES:
            found.add(m.group(1))
    return found


def classify_location(location):
    """
    Returns (loc_class, detail):
      'allowed'    -> in your area
      'out_of_area'-> a US location outside your area   (detail = state)
      'foreign'    -> outside the US
      'us_remote'  -> 'United States' / 'Remote' with no city: needs JD proof
      'unknown'    -> no location data at all
    """
    loc = (location or '').strip()
    if not loc:
        return 'unknown', ''
    low = loc.lower()
    if any(f in ' ' + low for f in FOREIGN):
        return 'foreign', loc
    states = _states_in(loc)
    if states:
        if states & ALLOWED_STATES:
            return 'allowed', ','.join(sorted(states))
        if states == {'TX'}:
            if any(c in low for c in ALLOWED_TX_CITIES):
                return 'allowed', 'TX'
            return 'out_of_area', 'TX (not Dallas/Houston)'
        return 'out_of_area', ','.join(sorted(states))
    if any(city in low for city in ALLOWED_TX_CITIES | {'atlanta', 'charlotte', 'raleigh',
            'tampa', 'orlando', 'miami', 'jacksonville', 'charleston', 'columbia'}):
        return 'allowed', loc
    for metro, st in METRO_STATES.items():
        if metro in low:
            return ('allowed' if st in ALLOWED_STATES else 'out_of_area'), st
    if any(m in low for m in US_REMOTE_MARKERS):
        return 'us_remote', loc
    return 'unknown', loc


def _residency_check(jd):
    """For remote jobs: find sentences restricting where you can live."""
    sents = [x for x in re.split(r'(?<=[.!?])\s+|\n', jd) if x.strip()]
    for i, sent in enumerate(sents):
        if re.search(RESIDENCY_TRIGGERS, sent, re.I):
            # state lists often follow on the next line/bullet
            states = _states_in(' '.join(sents[i:i + 2]))
            if not states:
                continue
            if states & ALLOWED_STATES:
                return None                  # restriction exists but includes you
            return 'residency_restricted:' + ','.join(sorted(states))
    return None


def _salary_low(s):
    if not s:
        return None
    t = s.lower().replace(',', '').replace('$', '')
    nums = re.findall(r'\d+\.?\d*', t)
    if not nums:
        return None
    low = float(nums[0])
    if re.search(r'\d\s*k\b', t):
        low *= 1000
    if '/hr' in t or 'hour' in t:
        low *= 2080
    return low


# ── MAIN ENTRY POINT ──────────────────────────────────────
def evaluate(job, jd_text=None):
    """
    job: dict with at least title, company, location
    jd_text: full job description if enrichment fetched it (None = not available)
    Returns {'filter_status', 'filter_reasons', 'loc_class', 'filter_version'}
    """
    title = (job.get('title') or '').strip()
    tl = title.lower()
    company = (job.get('company') or '').lower()
    reject, review = [], []

    # 1. Title
    if not title or any(re.search(p, tl) for p in JUNK_TITLE):
        reject.append('junk_title')
    else:
        for p in TITLE_EXCLUDE:
            m = re.search(p, tl)
            if m:
                reject.append('title_excluded:' + m.group(0).strip())
                break
        if not reject and not any(p in tl for p in TARGET_TITLES):
            if re.search(ANALYST_WORDS, tl):
                review.append('title_unlisted')
            else:
                reject.append('title_not_target')

    # 1b. Salary (only when posted)
    low = _salary_low(job.get('salary'))
    if low is not None and low < SALARY_FLOOR:
        reject.append(f'salary_below_floor:{int(low)}')

    # 2. Company
    if any(c in company for c in COMPANY_BLOCK):
        reject.append('company_blocked')

    # 3. Location (allowlist, fail closed)
    loc_class, detail = classify_location(job.get('location'))
    if loc_class == 'unknown':
        m = re.search(r'[A-Z][a-zA-Z .]+,\s*[A-Z]{2}\b', title)   # e.g. 'Data Analyst 2 - Minnetonka, MN'
        if m and _states_in(m.group(0)):
            loc_class, detail = classify_location(m.group(0))
    if loc_class == 'foreign':
        reject.append('foreign:' + detail)
    elif loc_class == 'out_of_area':
        if job.get('remote_search') and REMOTE_OUT_OF_AREA_POLICY == 'review' \
                and not (jd_text and _residency_check(jd_text)):
            review.append('remote_hq_elsewhere:' + detail)
        else:
            reject.append('out_of_area:' + detail)
    elif loc_class == 'unknown':
        review.append('location_unknown')
    elif loc_class == 'us_remote' and jd_text is None:
        review.append('remote_unverified')

    # 4. JD body
    if jd_text:
        for code, pat in JD_REJECT.items():
            if re.search(pat, jd_text, re.I):
                reject.append(code)
        if loc_class in ('us_remote', 'unknown'):
            r = _residency_check(jd_text)
            if r:
                reject.append(r)
        for code, pat in JD_REVIEW.items():
            if code == 'jd_term:onsite_or_hybrid' and loc_class != 'us_remote':
                continue    # hybrid in Atlanta is fine; hybrid on a 'remote' job is not
            if re.search(pat, jd_text, re.I):
                review.append(code)

    status = 'reject' if reject else ('review' if review else 'pass')
    return {
        'filter_status': status,
        'filter_reasons': reject + review,
        'loc_class': loc_class,
        'filter_version': FILTER_VERSION,
    }
