#!/usr/bin/env python3
"""
JSA Proactive Job Search — LinkedIn + Greenhouse.
Runs locally via Task Scheduler through run_proactive.bat (git pull first).

Rules live in jsa_filters.py, fetching in jsa_enrich.py, writes in jsa_store.py.
No filter lists and NO TOKEN in this file. Token comes from the JSA_GH_TOKEN
environment variable (set once with: setx JSA_GH_TOKEN yourtoken).
"""

import os, re, sys, json, time, random, base64
import urllib.request, urllib.parse
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

def todays_cities():
    dow = datetime.now().weekday()          # 0=Mon
    if dow in (0, 3):
        return ['Atlanta, GA', 'Charlotte, NC', 'Raleigh, NC', 'Columbia, SC', 'Charleston, SC']
    if dow in (1, 4):
        return ['Tampa, FL', 'Orlando, FL', 'Jacksonville, FL', 'Miami, FL']
    return ['Dallas, TX', 'Houston, TX']

STALE = ['week', 'month', '4 days', '5 days', '6 days']
MAX_FETCH = 250          # per run, LinkedIn posting fetches (each ~1-2.5s)

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
      'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'en-US,en;q=0.9'}


def uid():
    return hex(int(time.time() * 1000))[2:] + base64.urlsafe_b64encode(os.urandom(3)).decode()[:4]


def new_job(title, company, location, url, source, remote_search=False):
    return {'id': uid(), 'company': company or 'See posting', 'title': title,
            'location': location, 'salary': '', 'url': url, 'source': source,
            'viewed': False, 'dateAdded': datetime.now(timezone.utc).isoformat(), 'notes': '',
            'remote_search': remote_search}


# ── LINKEDIN ──────────────────────────────────────────────
def linkedin_ids(keywords, location):
    params = {'keywords': keywords, 'location': location, 'f_TPR': 'r259200',
              'sortBy': 'DD', 'start': '0'}
    if location.lower() == 'remote':
        params['geoId'] = '103644278'
        params['f_WT'] = '2'
    url = 'https://www.linkedin.com/jobs/search/?' + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
        html = r.read().decode('utf-8', errors='replace')
    return list(dict.fromkeys(re.findall(r'jobPosting:(\d+)', html)))[:20]


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
    rj, _ = jsa_store.read(token, jsa_store.REJECTS_FILE, {'rejects': []})
    # Skip anything already stored, dismissed, or previously rejected — no wasted fetches
    seen_u = {jsa_store.url_key(j) for j in data.get('jobs', []) + rj['rejects'] if j.get('url')}
    seen_u |= {jsa_store.url_key({'url': d}) for d in data.get('dismissed', [])}
    seen_a = {jsa_store.alt_key(j) for j in data.get('jobs', [])}
    print(f"Current: {len(data.get('jobs', []))} jobs, {len(rj['rejects'])} known rejects")

    keep, rejects, records, fetched = [], [], [], 0

    # LinkedIn
    searches = [(t, 'Remote') for t in TITLES] + [(t, c) for c in todays_cities() for t in TITLES]
    print(f"\nLinkedIn: {len(searches)} searches")
    for kw, loc in searches:
        try:
            ids = linkedin_ids(kw, loc)
        except Exception as e:
            print(f"  search error ({kw} / {loc}): {e}")
            continue
        added = 0
        for jid in ids:
            url = f'https://www.linkedin.com/jobs/view/{jid}/'
            uk = jsa_store.url_key({'url': url})
            if uk in seen_u or fetched >= MAX_FETCH:
                continue
            seen_u.add(uk)
            job = new_job('', '', '', url, 'LinkedIn', remote_search=(loc == 'Remote'))
            d = jsa_enrich.enrich(job, pause=random.uniform(1, 2.5))
            fetched += 1
            if not d:
                continue                                   # closed posting or fetch blocked
            if d.get('posted') and any(s in d['posted'] for s in STALE):
                continue
            jsa_enrich.apply_enrichment(job, d)
            job['salary'] = d.get('salary', '')
            ak = jsa_store.alt_key(job)
            if ak in seen_a:
                continue
            seen_a.add(ak)
            route(job, d['jd'], keep, rejects, records)
            added += 1
        print(f"  {kw} / {loc}: {added} evaluated")
        time.sleep(random.uniform(3, 7))
        if fetched >= MAX_FETCH:
            print(f"  fetch cap {MAX_FETCH} reached — stopping LinkedIn for this run")
            break

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
            'rules': jsa_filters.FILTER_VERSION, 'bySource': by_src, 'reasons': reasons}
    added = jsa_store.merge_jobs(token, keep, rejects, stat,
        f"JSA proactive {datetime.now():%Y-%m-%d %H:%M}: +{len(keep)} kept, {len(rejects)} rejected",
        set_last_run=False)
    print(f"\nWritten: {len(added)} added, {len(rejects)} rejected, {fetched} LinkedIn fetches")
    print(f"Done — {datetime.now():%H:%M:%S}\n")


if __name__ == '__main__':
    main()
