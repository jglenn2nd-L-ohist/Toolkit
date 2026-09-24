#!/usr/bin/env python3
"""
jsa_enrich.py — fetch the data that actually decides eligibility.

Email alerts and search cards carry title/company/location-label only.
Residency limits, clearance, and hybrid terms live in the JD body.

Supported:
  LinkedIn   -> public guest endpoint (company, title, location, full JD)
  Greenhouse -> public boards API (structured location + JD)
Everything else returns None and the job lands in Review, which is the honest answer.
"""

import re, json, time, html as htmlmod, urllib.request

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
      'Accept': 'text/html,application/xhtml+xml,application/json'}


def _get(url, timeout=10):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='replace')


def _clean(fragment):
    t = re.sub(r'<br\s*/?>|</p>|</li>', '\n', fragment)
    t = re.sub(r'<[^>]+>', ' ', t)
    t = htmlmod.unescape(t)
    return re.sub(r'[ \t]+', ' ', t).strip()


def linkedin(url):
    m = re.search(r'/jobs/view/(?:[^/?]*-)?(\d{6,})', url) or re.search(r'currentJobId=(\d+)', url)
    if not m:
        return None
    page = _get(f'https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}')
    def pick(pat):
        x = re.search(pat, page, re.S)
        return htmlmod.unescape(x.group(1)).strip() if x else ''
    desc = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', page, re.S)
    if not desc:
        return None      # posting closed/removed or page shape changed
    return {
        'title':    pick(r'top-card-layout__title[^>]*>\s*([^<]+)'),
        'company':  pick(r'topcard__org-name-link[^>]*>\s*([^<]+)'),
        'location': pick(r'topcard__flavor--bullet">\s*([^<]+)'),
        'jd':       _clean(desc.group(1)),
    }


def greenhouse(url):
    m = re.search(r'greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)', url) \
        or re.search(r'boards\.greenhouse\.io/([\w-]+).*?gh_jid=(\d+)', url)
    if not m:
        return None
    board, jid = m.group(1), m.group(2)
    d = json.loads(_get(f'https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}'))
    return {
        'title':    d.get('title', ''),
        'company':  '',
        'location': (d.get('location') or {}).get('name', ''),
        'jd':       _clean(htmlmod.unescape(d.get('content', ''))),
    }


def enrich(job, pause=1.2):
    """Returns dict(title, company, location, jd) or None. Never raises."""
    url = job.get('url', '')
    try:
        if 'linkedin.com' in url:
            out = linkedin(url)
        elif 'greenhouse.io' in url:
            out = greenhouse(url)
        else:
            return None
        time.sleep(pause)    # be polite; LinkedIn throttles bursts
        return out
    except Exception:
        return None


def apply_enrichment(job, data):
    """Fill gaps from enrichment without clobbering good existing values."""
    if not data:
        return job
    if data.get('location'):
        job['location'] = data['location']
    if data.get('company') and job.get('company') in ('', 'See posting', None):
        job['company'] = data['company']
    # Email-parsed titles bleed ('Business Analyst I United States'); trust the posting
    if data.get('title'):
        job['title'] = data['title']
    job['enriched'] = True
    return job
