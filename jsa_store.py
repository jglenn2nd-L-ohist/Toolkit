#!/usr/bin/env python3
"""
jsa_store.py — one storage layer for every writer (email scraper, proactive scraper, backfill).

Fixes the two-writers problem: every write re-reads the latest file, merges, and
retries on a SHA conflict instead of overwriting whoever wrote last.

Data repo is set by env var JSA_DATA_REPO so moving to a private repo is a one-line change.
"""

import os, re, json, base64, time, urllib.request, urllib.error
from datetime import datetime, timezone

DATA_REPO    = os.environ.get('JSA_DATA_REPO', 'jglenn2nd-L-ohist/Toolkit')
JOBS_FILE    = 'jsa_jobs.json'
REJECTS_FILE = 'jsa_rejects.json'
REJECTS_KEEP = 1500          # rolling window; rejects are for auditing, not forever
STATS_KEEP   = 60


def _api(path):
    return f'https://api.github.com/repos/{DATA_REPO}/contents/{path}'


def _req(token, url, method='GET', body=None):
    req = urllib.request.Request(url, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json',
                 'Content-Type': 'application/json', 'User-Agent': 'JSA'})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def read(token, path, default):
    try:
        meta = _req(token, _api(path))
        return json.loads(base64.b64decode(meta['content']).decode('utf-8')), meta['sha']
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return default, None
        raise


def write(token, path, data, sha, message):
    body = {'message': message,
            'content': base64.b64encode(json.dumps(data, indent=2).encode()).decode()}
    if sha:
        body['sha'] = sha
    return _req(token, _api(path), 'PUT', body)['content']['sha']


# ── KEYS ──────────────────────────────────────────────────
def url_key(j):
    url = (j.get('url') or '').strip().lower().rstrip('/')
    m = re.search(r'/jobs/view/(?:[^/?]*-)?(\d{6,})', url) or re.search(r'currentjobid=(\d+)', url)
    if m:
        return f'linkedin:{m.group(1)}'
    return url.split('?')[0]


def alt_key(j):
    return ((j.get('company') or '') + '|' + (j.get('title') or '')).lower().strip()


# ── MERGE-WRITE ───────────────────────────────────────────
def merge_jobs(token, new_jobs, rejects, run_stat, message, retries=4, replace_ids=None):
    """
    Add new_jobs to jsa_jobs.json, append rejects to jsa_rejects.json, record run_stat.
    replace_ids: set of job ids to REMOVE from the live file (used by backfill).
    Re-reads before each attempt so concurrent writers (the app, the other scraper) never lose data.
    """
    for attempt in range(retries):
        data, sha = read(token, JOBS_FILE, {'jobs': [], 'outreach': [], 'dismissed': []})
        jobs = data.get('jobs', [])
        if replace_ids:
            jobs = [j for j in jobs if j.get('id') not in replace_ids]
        dismissed = {d.lower().rstrip('/') for d in data.get('dismissed', [])}
        seen_u = {url_key(j) for j in jobs if j.get('url')} | {url_key({'url': d}) for d in dismissed}
        seen_a = {alt_key(j) for j in jobs}
        added = []
        for j in new_jobs:
            if url_key(j) in seen_u or alt_key(j) in seen_a:
                continue
            seen_u.add(url_key(j)); seen_a.add(alt_key(j)); added.append(j)

        now = datetime.now(timezone.utc).isoformat()
        data['jobs'] = added + jobs
        data['lastRun'] = now
        data['savedAt'] = now
        data['runStats'] = (data.get('runStats', []) + [run_stat])[-STATS_KEEP:]
        try:
            write(token, JOBS_FILE, data, sha, message)
            break
        except urllib.error.HTTPError as e:
            if e.code in (409, 422) and attempt < retries - 1:
                time.sleep(2 + attempt * 2)
                continue
            raise

    if rejects:
        for attempt in range(retries):
            rj, rsha = read(token, REJECTS_FILE, {'rejects': []})
            rj['rejects'] = (rejects + rj.get('rejects', []))[:REJECTS_KEEP]
            try:
                write(token, REJECTS_FILE, rj, rsha, message + ' (rejects)')
                break
            except urllib.error.HTTPError as e:
                if e.code in (409, 422) and attempt < retries - 1:
                    time.sleep(2 + attempt * 2)
                    continue
                raise
    return added


def summarize(records):
    """{source: {pass: n, review: n, reject: n}} + top reject reasons. For logs and runStats."""
    by_src, reasons = {}, {}
    for r in records:
        s = r.get('source', 'Other')
        by_src.setdefault(s, {'pass': 0, 'review': 0, 'reject': 0})
        by_src[s][r['filter_status']] += 1
        if r['filter_status'] != 'pass':
            for code in r.get('filter_reasons', []):
                key = code.split(':')[0]
                reasons[key] = reasons.get(key, 0) + 1
    return by_src, dict(sorted(reasons.items(), key=lambda x: -x[1]))


def print_summary(by_src, reasons):
    print('\n  Source         pass  review  reject   garbage%')
    for s, c in sorted(by_src.items()):
        tot = sum(c.values()) or 1
        print(f"  {s:<13}{c['pass']:>5}{c['review']:>8}{c['reject']:>8}{100*c['reject']/tot:>10.0f}%")
    print('  Top reasons:', ', '.join(f'{k}={v}' for k, v in list(reasons.items())[:8]) or 'none')
