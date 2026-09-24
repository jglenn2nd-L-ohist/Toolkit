#!/usr/bin/env python3
"""
JSA Gmail Scraper — reads job alert emails, enriches, filters via jsa_filters, writes via jsa_store.
All eligibility rules live in jsa_filters.py. Do not add filter lists here.
Runs daily via GitHub Actions.
"""

import os, re, json, base64, quopri, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import jsa_filters, jsa_enrich, jsa_store

# ── CONFIG ────────────────────────────────────────────────
SCOPES      = ['https://www.googleapis.com/auth/gmail.modify']
# Data repo / file paths live in jsa_store.py (env JSA_DATA_REPO)
JSA_LABEL   = 'JSA-Reviewed'

JOB_DOMAINS = [
    'linkedin.com/jobs', 'linkedin.com/comm/jobs',
    'glassdoor.com/job-listing', 'glassdoor.com/partner/jobListing',
    'glassdoor.com/apply', 'glassdoor.com/job',
    'indeed.com/viewjob', 'indeed.com/rc/clk', 'indeed.com/pagead/clk',
    'ziprecruiter.com/km/', 'ziprecruiter.com/ekm/', 'ziprecruiter.com/c/',
    'click.monster.com', 'monster.com/job',
    'builtin.com/job', 'builtinatlanta.com',
    'lever.co', 'greenhouse.io', 'workday.com', 'myworkdayjobs.com',
    'icims.com', 'smartrecruiters.com', 'jobvite.com', 'taleo.net',
    'successfactors.com', 'bamboohr.com', 'talent.aquent.com', 'remotehunter.com',
]

SKIP_URL = [
    '.png','.jpg','.gif','.svg','.ico','.woff',
    '/assets/','/images/','unsubscribe','optout',
    'mailto:','tel:','facebook.com','twitter.com','instagram.com',
    'privacy-policy','terms-of-service','manage-preferences',
    'fonts.googleapis','fonts.gstatic','email-preferences',
    '/account/login','/account/settings','form.jotform','feedback','survey',
]

JUNK_ANCHORS = {
    'apply','view job','view jobs','view all jobs','view jobs in last 7 days',
    '1-click apply','here','apply now','create','easy apply',
    'show me more','privacy policy','contact us','sign in','sign up',
    'your profile','unsubscribe.','unsubscribe','manage job alerts',
    'manage alerts','view all','see all jobs','manage my alerts','settings',
    'terms','help center','download app','get the app','view on website',
    'share your feedback','get more recommendations','job preferences','view job',

}

# Keep JOB_WORDS as a fallback for context extraction only
JOB_WORDS = ['analyst', 'analytics', 'engineer', 'consultant', 'specialist',
             'scientist', 'coordinator', 'intelligence', 'reporting']

SEARCH_QUERIES = [
    'subject:"job alert"',
    'subject:"job matches"',
    'subject:"jobs for you"',
    'subject:"new jobs"',
    'subject:"jobs in"',
    'subject:"is hiring"',
    'subject:"are hiring"',
    'subject:"apply now" (analyst OR data OR business OR operations)',
    'subject:"hiring" (analyst OR data OR business)',
    'subject:"analyst"',
    'subject:"data analyst"',
    'subject:"business analyst"',
    'subject:"business intelligence"',
    'subject:"analytics"',
    'subject:"daily remote job alert"',
    'subject:"recommended jobs"',
    'subject:"new opportunities"',
]

# ── HELPERS ───────────────────────────────────────────────
def detect_source(url):
    if 'linkedin.com' in url: return 'LinkedIn'
    if 'glassdoor.com' in url: return 'Glassdoor'
    if 'indeed.com' in url: return 'Indeed'
    if 'ziprecruiter.com' in url: return 'ZipRecruiter'
    if 'builtin.com' in url: return 'Builtin'
    if 'monster.com' in url: return 'Monster'
    if 'aquent.com' in url: return 'Recruiter'
    return 'Other'

def uid():
    return hex(int(time.time() * 1000))[2:] + base64.urlsafe_b64encode(os.urandom(3)).decode()[:4]

# ── GMAIL ─────────────────────────────────────────────────
def get_gmail_service():
    token_data = os.environ.get('GMAIL_TOKEN')
    if not token_data:
        raise RuntimeError('GMAIL_TOKEN not set')
    creds = Credentials.from_authorized_user_info(json.loads(token_data), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError('Invalid credentials')
    return build('gmail', 'v1', credentials=creds), creds

def get_or_create_label(service, name):
    labels = service.users().labels().list(userId='me').execute()
    for l in labels.get('labels', []):
        if l['name'] == name:
            return l['id']
    created = service.users().labels().create(
        userId='me', body={'name': name, 'labelListVisibility': 'labelShow', 'messageListVisibility': 'show'}
    ).execute()
    return created['id']

# ── EMAIL PARSING ─────────────────────────────────────────
def decode_body(payload):
    html_parts, text_parts = [], []
    def walk(part):
        mime = part.get('mimeType', '')
        data = part.get('body', {}).get('data', '')
        if data:
            try:
                raw = base64.urlsafe_b64decode(data + '==')
                try:
                    decoded = quopri.decodestring(raw).decode('utf-8', errors='replace')
                except Exception:
                    decoded = raw.decode('utf-8', errors='replace')
                if 'html' in mime:
                    html_parts.append(decoded)
                else:
                    text_parts.append(decoded)
            except Exception:
                pass
        for sub in part.get('parts', []):
            walk(sub)
    walk(payload)
    return ' '.join(html_parts), ' '.join(text_parts)

def extract_jobs_from_email(body_html, body_text, subject):
    jobs = []

    # Build anchor pairs: (url, anchor_text)
    anchor_pairs = []
    for m in re.finditer(r'<a\s([^>]+)>(.*?)</a>', body_html, re.DOTALL):
        attrs, inner = m.group(1), m.group(2)
        href_m = re.search(r'href=["\']([^"\'<>]+)["\']', attrs)
        if not href_m:
            continue
        href = href_m.group(1).rstrip('.,;)&').replace('&amp;', '&')
        if any(s in href.lower() for s in SKIP_URL):
            continue
        # Decode Builtin AWS tracking
        if 'awstrack.me/L0/' in href:
            try:
                href = urllib.parse.unquote(href.split('/L0/')[1])
            except Exception:
                pass
        text = re.sub(r'<[^>]+>', ' ', inner)
        text = text.replace('&amp;','&').replace('&nbsp;',' ').replace('&#39;',"'")
        text = text.replace('&#x2F;','/').replace('&lt;','<').replace('&gt;','>')
        text = text.replace('\u2605','').replace('\u2192','').replace('\u2013','-')
        text = re.sub(r'\s+', ' ', text).strip()
        anchor_pairs.append((href, text))

    seen_urls, seen_alts = set(), set()

    for i, (href, text) in enumerate(anchor_pairs):
        if not any(d in href for d in JOB_DOMAINS):
            continue
        base = href.split('?')[0].lower().rstrip('/')
        if base in seen_urls:
            continue

        title, company, location, salary = '', '', '', ''

        # Get title from anchor text
        tl = text.lower().strip('.→')
        if text and len(text) > 4 and tl not in JUNK_ANCHORS and not tl.startswith('your job alert for'):

            if 'glassdoor.com' in href:
                # Remove company + rating prefix: "Cisco 4.1 ★ Title" or "Cisco 4.1 Title"
                if '★' in text or '\u2605' in text:
                    _parts = re.split(r'[★\u2605]\s*', text, maxsplit=1)
                    _after = _parts[1] if len(_parts) > 1 else text
                else:
                    # No star — strip leading "Company X.X " pattern
                    _after = re.sub(r'^[\w\s&,\.]+?\d+\.\d+\s*', '', text).strip()
                    if not _after or len(_after) < 4:
                        _after = text  # fallback if strip went too far
                # Strip "United States", "US" from end
                _after = re.sub(r'\s+United States$', '', _after, flags=re.IGNORECASE).strip()
                _after = re.sub(r'\s+\(Full Time\)', '', _after, flags=re.IGNORECASE).strip()
                _after = re.sub(r'\s+\(Part Time\)', '', _after, flags=re.IGNORECASE).strip()
                # Strip "and X more jobs"
                _after = re.sub(r'\s+and\s+\d+\s+more.*$', '', _after, flags=re.IGNORECASE).strip()
                # Strip salary
                _sal_m = re.search(r'\s+(\$[\d,K\.\-]+(?:\s*[-–]\s*\$[\d,K\.]+)?)', _after)
                if _sal_m:
                    salary = _sal_m.group(1).strip()
                    _after = _after[:_sal_m.start()].strip()
                # Strip Remote/Hybrid
                _after = re.sub(r'\s+(?:Remote|Hybrid)$', '', _after, flags=re.IGNORECASE).strip()
                # Strip trailing city/state: find ", XX" at end and remove preceding city word
                _st_m = re.search(r',\s*(GA|FL|TX|NC|SC|LA|AL|NY|CA|IL|OH|PA|MA|CO|WA|MN|WI|MO|KY|IN|OK|KS|NE|IA|UT|AZ|NV|ID|MT|ND|SD|WY|NM|AK|HI|ME|VT|NH|RI|CT|DE|MD|DC|NJ|VA|MI|TN)$', _after)
                if _st_m:
                    _before = _after[:_st_m.start()]
                    _city_m = re.search(r'\s+\S+$', _before)
                    if _city_m:
                        _city = _city_m.group().strip()
                        _job_wds = ['analyst','engineer','consultant','data','business',
                                    'financial','operations','marketing','intelligence',
                                    'systems','solutions','revenue','strategy','pricing']
                        if not any(w in _city.lower() for w in _job_wds):
                            _after = _before[:_city_m.start()].strip()
                title = _after.strip()

            elif 'builtin.com' in href:
                # Anchor text is 'Company Title Hybrid ...'. Builtin's URL slug IS the title
                # (/job/data-analyst-poker/123), so find where the slug starts in the text.
                title = ''
                sm = re.search(r'builtin\.com/job/([a-z0-9-]+)/\d+', href)
                if sm:
                    slug = sm.group(1)
                    norm = lambda x: re.sub(r'[^a-z0-9]+', '-', x.lower()).strip('-')
                    for wm in re.finditer(r'\S+', text):
                        if wm.start() == 0:
                            continue
                        if norm(text[wm.start():]).startswith(slug[:min(len(slug), 24)]):
                            company = text[:wm.start()].strip()
                            rest = text[wm.start():]
                            # cut the title where the slug ends
                            n, words = 0, rest.split()
                            for k in range(1, len(words) + 1):
                                if norm(' '.join(words[:k])) == slug or len(norm(' '.join(words[:k]))) >= len(slug):
                                    n = k; break
                            title = ' '.join(words[:n]) if n else rest
                            break
                if not title:
                    title = text

            else:
                title = text

        # LinkedIn: extract company from subject line for single-job alerts
        if 'linkedin.com' in href and (not company or company == 'See posting'):
            # Subject formats:
            # '"Title" at Company - View jobs...'
            # 'Title at Company posted on...'
            # '"Title at Company"'
            co_match = re.search(r'at\s+([^\-\n]+?)(?:\s*-|\s+posted|\s+in\s)', subject, re.IGNORECASE)

            if co_match:
                company = co_match.group(1).strip().rstrip('.,')

        # Monster: title may be in next meaningful anchor
        if (not title or len(title) < 4) and 'monster.com' in href:
            for _h, next_text in anchor_pairs[i+1:i+6]:
                nt = next_text.strip()
                if (len(nt) > 8 and nt.lower() not in JUNK_ANCHORS
                        and any(w in nt.lower() for w in JOB_WORDS)):
                    title = nt
                    break

        # Fallback: context search
        if not title or len(title) < 4:
            idx = body_html.find(href[:50])
            if idx > -1:
                ctx = re.sub(r'<[^>]+>', ' ', body_html[max(0,idx-400):idx+200])
                ctx = re.sub(r'\s+', ' ', ctx).strip()
                tm = re.search(
                    r'([A-Z][A-Za-z\s&,\-/]{8,60}(?:Analyst|Engineer|Consultant|Specialist|Developer|Scientist|Associate|Coordinator|Operations|Intelligence|Research|Advisor))',
                    ctx)
                if tm:
                    title = tm.group(1).strip()

        if not title or len(title) < 4:
            continue
        if title.lower().strip('.→') in JUNK_ANCHORS:
            continue
        # Prescreen only: is this anchor a job link at all? Eligibility is decided
        # later by jsa_filters.evaluate(), with reasons recorded.
        if not re.search(r'analyst|analytics|intelligence', title, re.I):
            continue

        alt_key = title.lower().strip()
        if alt_key in seen_alts:
            continue

        seen_urls.add(base)
        seen_alts.add(alt_key)

        jobs.append({
            'id': uid(),
            'company': company or 'See posting',
            'title': title,
            'location': location,
            'salary': salary,
            'url': href,
            'source': detect_source(href),
            'status': 'New',
            'viewed': False,
            'dateAdded': datetime.now(timezone.utc).isoformat(),
            'notes': ''
        })

    return jobs

# ── MAIN ──────────────────────────────────────────────────
MAX_ENRICH = 150   # per run; LinkedIn throttles large bursts

def main():
    print(f"JSA Scraper starting — {datetime.now(timezone.utc).isoformat()}  rules v{jsa_filters.FILTER_VERSION}")

    github_token = os.environ.get('JSA_GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not github_token:
        raise RuntimeError('JSA_GH_TOKEN not set')

    current_data, _ = jsa_store.read(github_token, jsa_store.JOBS_FILE, {'jobs': []})
    rejects_data, _ = jsa_store.read(github_token, jsa_store.REJECTS_FILE, {'rejects': []})
    existing_jobs = current_data.get('jobs', [])
    last_run = current_data.get('lastRun')
    print(f"Current: {len(existing_jobs)} jobs, lastRun: {last_run}")

    seen_u = {jsa_store.url_key(j) for j in existing_jobs + rejects_data['rejects'] if j.get('url')}
    seen_u |= {jsa_store.url_key({'url': d}) for d in current_data.get('dismissed', [])}
    seen_a = {jsa_store.alt_key(j) for j in existing_jobs}

    gmail_service, _ = get_gmail_service()
    label_id = get_or_create_label(gmail_service, JSA_LABEL)

    since = (datetime.fromisoformat(last_run.replace('Z', '+00:00')) if last_run
             else datetime.now(timezone.utc) - timedelta(days=30))
    after_date = since.strftime('%Y/%m/%d')
    print(f"Searching emails after: {after_date}")

    all_message_ids = {}
    for query in SEARCH_QUERIES:
        try:
            result = gmail_service.users().messages().list(
                userId='me', q=f'{query} after:{after_date}', maxResults=100).execute()
            for m in result.get('messages', []):
                all_message_ids[m['id']] = True
        except Exception as e:
            print(f"  Query failed '{query[:40]}': {e}")
    print(f"Total unique messages: {len(all_message_ids)}")

    candidates, duped = [], 0
    for msg_id in all_message_ids:
        try:
            msg = gmail_service.users().messages().get(userId='me', id=msg_id, format='full').execute()
            headers = {h['name']: h['value'] for h in msg['payload'].get('headers', [])}
            subject = headers.get('Subject', '')
            body_html, body_text = decode_body(msg['payload'])
            for j in extract_jobs_from_email(body_html, body_text, subject):
                uk, ak = jsa_store.url_key(j), jsa_store.alt_key(j)
                if uk in seen_u or ak in seen_a:
                    duped += 1
                    continue
                seen_u.add(uk); seen_a.add(ak)
                candidates.append(j)
            if msg.get('threadId'):
                gmail_service.users().threads().modify(
                    userId='me', id=msg['threadId'], body={'addLabelIds': [label_id]}).execute()
        except Exception as e:
            print(f"  Error {msg_id}: {e}")
    print(f"Candidates: {len(candidates)}  (duplicates skipped: {duped})")

    # Enrich -> evaluate -> route
    keep, rejects, records, enriched = [], [], [], 0
    for j in candidates:
        data = None
        if enriched < MAX_ENRICH:
            data = jsa_enrich.enrich(j)
            if data:
                enriched += 1
                jsa_enrich.apply_enrichment(j, data)
        verdict = jsa_filters.evaluate(j, data['jd'] if data else None)
        j.update(verdict)
        records.append(j)
        if verdict['filter_status'] == 'reject':
            rejects.append({k: j.get(k) for k in ('title', 'company', 'location', 'url', 'source',
                            'filter_reasons', 'filter_version', 'dateAdded')})
        else:
            j['status'] = 'New' if verdict['filter_status'] == 'pass' else 'Review'
            keep.append(j)

    by_src, reasons = jsa_store.summarize(records)
    jsa_store.print_summary(by_src, reasons)
    run_stat = {'at': datetime.now(timezone.utc).isoformat(), 'runner': 'email',
                'rules': jsa_filters.FILTER_VERSION, 'bySource': by_src, 'reasons': reasons}

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    added = jsa_store.merge_jobs(github_token, keep, rejects, run_stat,
        f'JSA email harvest {today}: +{len(keep)} kept, {len(rejects)} rejected')
    print(f"Written: {len(added)} added, {len(rejects)} rejected, enriched {enriched}")
    print(f"Done — {datetime.now(timezone.utc).isoformat()}")


if __name__ == '__main__':
    main()
