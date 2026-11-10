#!/usr/bin/env python3
"""
JSA Proactive Job Search — LinkedIn + Greenhouse.
Runs locally via Task Scheduler through run_proactive.bat (git pull first).

Rules live in jsa_filters.py, fetching in jsa_enrich.py, writes in jsa_store.py.
No filter lists and NO TOKEN in this file. Token comes from the JSA_GH_TOKEN
environment variable (set once with: setx JSA_GH_TOKEN yourtoken).
"""

import os, re, sys, json, time, random, base64
import html as htmlmod
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

import jsa_filters, jsa_enrich, jsa_store

# ── SEARCH PLAN ───────────────────────────────────────────
GREENHOUSE_COMPANIES = [
    'salesloft', 'calendly', 'roofstock', 'affirm', 'charterup', 'salesmsg', 'samsara',
    'lattice', 'sonic', 'cardlytics', 'fullstory', 'terminus', 'stord', 'sharecare',
    'aprio', 'groundfloor', 'oncohealth', 'springhealth', 'beazer-homes', 'inspire-brands',
    'secureworks', 'wellstar', 'navicent', 'piedmont', 'manhattan-associates', 'americold',
    'americold-logistics', 'cox', 'cox-enterprises', 'mckesson', 'genuine-parts', 'invesco',
    'equifax', 'nuveen', 'cousins-properties', 'pindrop', 'greensky', 'kabbage',
    'brightspring', 'homestar-financial', 'intellicheck', 'cvent', 'beazer',
]

TITLES = [
    'data analyst', 'business analyst', 'operations analyst', 'business intelligence analyst',
    'marketing analyst', 'strategy analyst', 'analytics engineer', 'reporting analyst',
    'product analyst', 'systems analyst', 'supply chain analyst',
]

# ── ONE DAILY RUN (10-16) ────────────────────────────────
# Atlanta and Remote are searched every weekday; the other markets keep their rotation.
# Each city's time window reaches back to that city's previous scheduled search (+6h
# overlap), so nothing falls in a gap between searches. Weekday numbers: 0=Mon ... 4=Fri.
CITY_DAYS = {
    'Atlanta, GA':    [0, 1, 2, 3, 4],
    'Charlotte, NC':  [0, 3], 'Raleigh, NC': [0, 3], 'Columbia, SC': [0, 3], 'Charleston, SC': [0, 3],
    'Tampa, FL':      [1, 4], 'Orlando, FL': [1, 4], 'Jacksonville, FL': [1, 4], 'Miami, FL': [1, 4],
    'Dallas, TX':     [2],    'Houston, TX': [2],
    'Remote':         [0, 1, 2, 3, 4],
}
OVERLAP_HOURS = 6
MAX_BUSINESS_DAYS = 3            # 10-16: never look back more than 3 weekdays; weekend hours don't count
ADHOC_WINDOW_HOURS = 72          # app button runs / days a city isn't scheduled


def business_cap_hours(now=None, bdays=MAX_BUSINESS_DAYS):
    """Clock hours needed to reach back `bdays` weekdays from now, skipping Sat/Sun.
    Tue 9am -> 72h. Mon 9am -> 120h on the clock (Fri+Thu+Wed, the weekend is skipped)."""
    from datetime import timedelta
    now = now or datetime.now()
    need, k = bdays * 24, 0
    while need > 0:
        k += 1
        if (now - timedelta(hours=k)).weekday() < 5:
            need -= 1
    return k


def window_hours(city, now=None):
    """Hours since this city's previous scheduled search (+overlap), capped at 3 weekdays."""
    now = now or datetime.now()
    dow, days, cap = now.weekday(), CITY_DAYS.get(city, []), business_cap_hours(now)
    if dow not in days:
        return min(ADHOC_WINDOW_HOURS, cap)
    for back in range(1, 8):
        if (dow - back) % 7 in days:
            return min(back * 24 + OVERLAP_HOURS, cap)
    return cap


def todays_cities(dow=None):
    """Scheduled cities for today, Atlanta first (home market gets the budget first)."""
    dow = datetime.now().weekday() if dow is None else dow
    return [c for c, d in CITY_DAYS.items() if dow in d and c != 'Remote']


REGIONS = {
    'atl-carolinas': ['Atlanta, GA', 'Charlotte, NC', 'Raleigh, NC', 'Columbia, SC', 'Charleston, SC'],
    'florida':       ['Tampa, FL', 'Orlando, FL', 'Jacksonville, FL', 'Miami, FL'],
    'texas':         ['Dallas, TX', 'Houston, TX'],
}


def search_plan():
    """Region comes from JSA_REGION (set by the app's Run button via GitHub Actions) or argv.
    rotation = today's cities + Remote (the scheduled default); a named region = only those
    cities; remote = Remote/US only."""
    region = (os.environ.get('JSA_REGION') or (sys.argv[1] if len(sys.argv) > 1 else 'rotation')).strip().lower()
    if region == 'remote':
        return region, [], True
    if region in REGIONS:
        return region, REGIONS[region], False
    return 'rotation', todays_cities(), datetime.now().weekday() in CITY_DAYS['Remote']


MAX_FETCH = 250          # per run, LinkedIn posting fetches (each ~1-2.5s)
EXTRA_PAGES = 4          # per search, beyond the first 60 results (10 results per extra page)
EXTRA_PAGE_BUDGET = 120  # per run, total extra page requests
GUARD_HOURS = 6          # a second local run within this many hours of the last one exits
                         # (stops wake-up catch-up runs from creating a second review batch)


def posted_days(text):
    """'3 days ago' -> 3, '2 weeks ago' -> 14, '5 hours ago' -> 0. None if unknown."""
    m = re.search(r'(\d+)\s+(minute|hour|day|week|month)', text or '')
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return {'minute': 0, 'hour': 0, 'day': n, 'week': 7 * n, 'month': 30 * n}[unit]

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
      'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'en-US,en;q=0.9'}


def uid():
    return hex(int(time.time() * 1000))[2:] + base64.urlsafe_b64encode(os.urandom(3)).decode()[:4]


def new_job(title, company, location, url, source, remote_search=False):
    return {'id': uid(), 'company': company or 'See posting', 'title': title,
            'location': location, 'salary': '', 'url': url, 'source': source,
            'viewed': False, 'dateAdded': datetime.now(timezone.utc).isoformat(), 'notes': '',
            'remote_search': remote_search, 'via': 'proactive'}


# ── LINKEDIN ──────────────────────────────────────────────
def _parse_cards(page, seen):
    cards = []
    for chunk in re.split(r'(?=<div[^>]+data-entity-urn="urn:li:jobPosting:)', page)[1:]:
        jid = re.search(r'jobPosting:(\d+)', chunk).group(1)
        if jid in seen:
            continue
        seen.add(jid)
        def pick(pat):
            m = re.search(pat, chunk)
            return htmlmod.unescape(m.group(1)).strip() if m else ''
        cards.append({'id': jid,
                      'title': pick(r'base-search-card__title[^>]*>\s*([^<]+)'),
                      'company': pick(r'base-search-card__subtitle[^>]*>\s*(?:<a[^>]*>)?\s*([^<]+)'),
                      'location': pick(r'job-search-card__location[^>]*>\s*([^<]+)')})
    return cards


def _get_page(url):
    """GET with one slow retry on rate limiting (HTTP 429)."""
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
                return r.read().decode('utf-8', errors='replace')
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 1:
                print('    rate limited — waiting 60s', flush=True)
                time.sleep(60)
                continue
            raise


class Throttle:
    extra_used = 0
    stopped = False


def linkedin_cards(keywords, location, hours):
    """First 60 results from the search page, then up to EXTRA_PAGES more pages of 10.
    Returns (cards, complete) — complete=True means every job in the window was seen."""
    params = {'keywords': keywords, 'location': location, 'f_TPR': f'r{int(hours * 3600)}',
              'sortBy': 'DD', 'start': '0'}
    if location.lower() == 'remote':
        params['geoId'] = '103644278'
        params['f_WT'] = '2'
    seen = set()
    cards = _parse_cards(_get_page('https://www.linkedin.com/jobs/search/?' + urllib.parse.urlencode(params)), seen)
    if len(cards) < 50:
        return cards, True                       # the window held fewer jobs than one page
    for i in range(EXTRA_PAGES):
        if Throttle.stopped or Throttle.extra_used >= EXTRA_PAGE_BUDGET:
            return cards, False
        params['start'] = str(60 + 10 * i)
        time.sleep(random.uniform(2, 4))
        Throttle.extra_used += 1
        try:
            more = _parse_cards(_get_page('https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?'
                                          + urllib.parse.urlencode(params)), seen)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                Throttle.stopped = True          # stop extra paging for the rest of this run
                print('    still rate limited — no more extra pages this run', flush=True)
            return cards, False
        cards += more
        if len(more) < 10:
            return cards, True                   # reached the end of the results
    return cards, False


# ── GREENHOUSE ────────────────────────────────────────────
def greenhouse_jobs(slug):
    url = f'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false'
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'JSA'}),
                                timeout=8) as r:
        return json.loads(r.read()).get('jobs', [])


# ── ROUTING ───────────────────────────────────────────────
def route(job, jd, keep, rejects, records):
    v = jsa_filters.evaluate(job, jd)
    job.update(v)
    records.append(job)
    if v['filter_status'] == 'reject':
        rejects.append({k: job.get(k) for k in ('title', 'company', 'location', 'url', 'source',
                        'filter_reasons', 'filter_version', 'dateAdded')})
    else:
        job['status'] = 'New' if v['filter_status'] == 'pass' else 'Review'
        keep.append(job)


def main():
    print(f"\nJSA Proactive starting — {datetime.now():%Y-%m-%d %H:%M}  rules v{jsa_filters.FILTER_VERSION}")
    token = os.environ.get('JSA_GH_TOKEN')
    if not token:
        sys.exit('JSA_GH_TOKEN is not set. Run once: setx JSA_GH_TOKEN yourtoken  (then open a new window)')

    data, _ = jsa_store.read(token, jsa_store.JOBS_FILE, {'jobs': []})

    # One review batch a day: a local run soon after the last one exits. App-button runs on
    # GitHub (GITHUB_ACTIONS is set there) always run.
    if not os.environ.get('GITHUB_ACTIONS'):
        last = [r['at'] for r in data.get('runStats', []) if r.get('runner') == 'proactive']
        if last:
            ago = (datetime.now(timezone.utc) - datetime.fromisoformat(max(last).replace('Z', '+00:00'))).total_seconds() / 3600
            if ago < GUARD_HOURS:
                print(f"Last proactive run was {ago:.1f}h ago (< {GUARD_HOURS}h) — skipping to avoid a second batch.")
                return

    rj, _ = jsa_store.read(token, jsa_store.REJECTS_FILE, {'rejects': []})
    # Skip anything already stored, dismissed, or previously rejected — no wasted fetches
    seen_u = {jsa_store.url_key(j) for j in data.get('jobs', []) + rj['rejects'] if j.get('url')}
    seen_u |= {jsa_store.url_key({'url': d}) for d in data.get('dismissed', [])}
    seen_a = {jsa_store.alt_key(j) for j in data.get('jobs', [])} | jsa_store.dismissed_keys(data)
    print(f"Current: {len(data.get('jobs', []))} jobs, {len(rj['rejects'])} known rejects")

    keep, rejects, records, fetched = [], [], [], 0

    # LinkedIn — local cities first, Remote last: Remote results are mostly out-of-area,
    # so they should never consume the budget before your real markets are covered.
    region, cities, with_remote = search_plan()
    searches = [(t, c) for c in cities for t in TITLES] + ([(t, 'Remote') for t in TITLES] if with_remote else [])
    print(f"\nRegion: {region} — LinkedIn: {len(searches)} searches")
    card_rejects, complete, cards_seen = 0, 0, 0
    for kw, loc in searches:
        hours = window_hours(loc) if region == 'rotation' else ADHOC_WINDOW_HOURS
        try:
            cards, full = linkedin_cards(kw, loc, hours)
        except Exception as e:
            print(f"  search error ({kw} / {loc}): {e}")
            continue
        complete += full
        cards_seen += len(cards)
        added = 0
        for c in cards:
            url = f'https://www.linkedin.com/jobs/view/{c["id"]}/'
            uk = jsa_store.url_key({'url': url})
            if uk in seen_u:
                continue
            seen_u.add(uk)
            job = new_job(c['title'], c['company'], c['location'], url, 'LinkedIn',
                          remote_search=(loc == 'Remote'))
            ak = jsa_store.alt_key(job)
            if ak in seen_a:
                continue
            seen_a.add(ak)
            # Decide from the card first; only open postings that could still pass
            if jsa_filters.evaluate(job, None)['filter_status'] == 'reject':
                route(job, None, keep, rejects, records)
                card_rejects += 1
                continue
            if fetched >= MAX_FETCH:
                continue
            d = jsa_enrich.enrich(job, pause=random.uniform(1, 2.5))
            fetched += 1
            if not d:
                continue                                   # closed posting or fetch blocked
            age = posted_days(d.get('posted'))
            if age is not None and age * 24 > hours + 24:
                continue                                   # older than this search's window
            jsa_enrich.apply_enrichment(job, d)
            job['salary'] = d.get('salary', '')
            route(job, d['jd'], keep, rejects, records)
            added += 1
        print(f"  {kw} / {loc} [{hours}h]: {len(cards)} cards{' (all)' if full else ''}, {added} opened", flush=True)
        time.sleep(random.uniform(3, 7))
    print(f"  {cards_seen} cards seen; {complete}/{len(searches)} searches saw every job in their window; "
          f"{Throttle.extra_used} extra pages")
    print(f"  {card_rejects} rejected from search cards without opening; {fetched} postings opened")
    if fetched >= MAX_FETCH:
        print(f"  fetch cap {MAX_FETCH} reached")

    # Greenhouse
    print("\nGreenhouse boards...")
    for slug in GREENHOUSE_COMPANIES:
        try:
            gjobs = greenhouse_jobs(slug)
        except Exception:
            continue
        n = 0
        for g in gjobs:
            title = g.get('title', '')
            if not re.search(r'analyst|analytics|intelligence|insights', title, re.I):
                continue
            job = new_job(title, slug.replace('-', ' ').title(),
                          (g.get('location') or {}).get('name', ''), g.get('absolute_url', ''),
                          'Greenhouse')
            if jsa_store.url_key(job) in seen_u or jsa_store.alt_key(job) in seen_a:
                continue
            seen_u.add(jsa_store.url_key(job)); seen_a.add(jsa_store.alt_key(job))
            # cheap pre-check: skip the JD fetch if title/location already reject it
            if jsa_filters.evaluate(job, None)['filter_status'] == 'reject':
                route(job, None, keep, rejects, records)
                continue
            try:
                d = jsa_enrich.greenhouse_by_id(slug, g.get('id'))
            except Exception:
                d = None
            route(job, d['jd'] if d else None, keep, rejects, records)
            n += 1
        if n:
            print(f"  {slug}: {n} evaluated")
        time.sleep(random.uniform(0.5, 1.5))

    by_src, reasons = jsa_store.summarize(records)
    jsa_store.print_summary(by_src, reasons)

    if not records:
        print("\nNothing new to evaluate — GitHub unchanged.")
        return
    stat = {'at': datetime.now(timezone.utc).isoformat(), 'runner': 'proactive',
            'rules': jsa_filters.FILTER_VERSION, 'bySource': by_src, 'reasons': reasons,
            'found': len(records), 'opened': fetched, 'region': region,
            'cards': cards_seen, 'searches': len(searches), 'complete': complete}
    added = jsa_store.merge_jobs(token, keep, rejects, stat,
        f"JSA proactive {datetime.now():%Y-%m-%d %H:%M}: +{len(keep)} kept, {len(rejects)} rejected",
        set_last_run=False)
    print(f"\nWritten: {len(added)} added, {len(rejects)} rejected, {fetched} LinkedIn fetches")
    print(f"Done — {datetime.now():%H:%M:%S}\n")


if __name__ == '__main__':
    main()
