#!/usr/bin/env python3
"""
JSA Gmail Scraper — reads job alert emails, writes to GitHub.
Runs daily via GitHub Actions.
"""

import os, re, json, base64, quopri, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── CONFIG ────────────────────────────────────────────────
SCOPES      = ['https://www.googleapis.com/auth/gmail.modify']
GITHUB_REPO = 'jglenn2nd-L-ohist/Toolkit'
GITHUB_FILE = 'jsa_jobs.json'
GITHUB_API  = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}'
JSA_LABEL   = 'JSA-Reviewed'
LOOKBACK_DAYS = 3

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

EXCLUDE_WORDS = [
    'senior','sr.','sr ','lead','principal','staff',
    'intern','internship','co-op','coop',
    'behavioral health','substance abuse','mental health counselor',
    'director','vp ','vice president','head of','manager,',
    'summer 202','spring 202','early career intern',
    'collections specialist','credit and collections',
    'cybersecurity','cyber security','information security','infosec',
    'network security','threat intelligence','soc analyst',
    'penetration test','vulnerability','firewall','siem',
    'administrative','purchasing','buyer ','procurement specialist',
    'logistics coordinator','warehouse','full-stack','full stack',
    'software engineer','devops','cloud engineer','platform engineer',
]

# Exact target role patterns - title must contain one of these
TARGET_TITLE_PATTERNS = [
    'data analyst', 'business analyst', 'operations analyst',
    'financial analyst', 'marketing analyst', 'business intelligence',
    'bi analyst', 'analytics engineer', 'revenue analyst',
    'strategy analyst', 'reporting analyst', 'insights analyst',
    'systems analyst', 'pricing analyst', 'supply chain analyst',
    'inventory analyst', 'logistics analyst', 'forecast analyst',
    'planning analyst', 'workforce analyst', 'compensation analyst',
    'hr analyst', 'people analyst', 'product analyst',
    'commercial analyst', 'portfolio analyst', 'budget analyst',
    'credit analyst', 'actuarial analyst', 'tax analyst',
    'audit analyst', 'quality analyst', 'process analyst',
    'performance analyst', 'intelligence analyst', 'data quality',
    'business systems analyst', 'revenue operations analyst',
    'sales analyst', 'category analyst', 'campaign analyst',
    'market analyst', 'research analyst', 'customer analyst',
    'data science', 'analytics consultant', 'analytics specialist',
]

# Keep JOB_WORDS as a fallback for context extraction only
JOB_WORDS = ['analyst', 'analytics', 'engineer', 'consultant', 'specialist',
             'scientist', 'coordinator', 'intelligence', 'reporting']

TARGET_STATES = [
    'georgia',' ga,',', ga','atlanta','norcross','alpharetta','duluth','marietta',
    'florida',' fl,',', fl','miami','orlando','tampa','jacksonville',
    'north carolina',' nc,',', nc','charlotte','raleigh',
    'south carolina',' sc,',', sc','charleston','columbia',
    'louisiana',' la,',', la','new orleans','baton rouge',
    'alabama',' al,',', al','birmingham','huntsville',
    'texas',' tx,',', tx','houston','dallas','austin','san antonio',
    'remote','united states','hybrid','us only','nationwide',
]

OUT_OF_AREA_MARKERS = [
    ' mi',' michigan','warren, mi','detroit','grand rapids',
    ' ny',' new york','long island','queens','brooklyn','manhattan',
    ' ma',' massachusetts','boston','cambridge',
    ' ca',' california','los angeles','san francisco','san diego',
    ' il',' illinois','chicago',
    ' oh',' ohio','columbus','cleveland','cincinnati',
    ' pa',' pennsylvania','philadelphia','pittsburgh',
    ' nj',' new jersey',' md',' maryland','baltimore',
    ' co',' colorado','denver',' wa state','seattle',
    ' mn',' minnesota','minneapolis',' wi',' wisconsin',
    ' mo',' missouri',' ky',' kentucky',' in',' indiana',
    ' ok',' oklahoma',' ks',' kansas',' ne',' nebraska',
    ' ia',' iowa',' ut',' utah',' az',' arizona','phoenix',
    ' nv',' nevada',' id',' idaho',' mt',' montana',
    ' nd',' nd ',' sd',' south dakota',' wy',' wyoming',
    ' nm',' new mexico',' ak',' alaska',' hi',' hawaii',
    ' me',' maine',' vt',' vermont',' nh',' new hampshire',
    ' ri',' rhode island',' ct',' connecticut',' de',' delaware',
    'washington dc','washington d.c.',
]

SEARCH_QUERIES = [
    'subject:"job alert" analyst',
    'subject:"job alert" data',
    'subject:"job alert" business',
    'subject:"jobs for you"',
    'subject:"new jobs" analyst',
    'subject:"jobs in" analyst',
    'subject:"is hiring" analyst',
    'subject:"are hiring" analyst',
    'subject:"hiring" analyst atlanta',
    'subject:"data analyst" (job OR hiring OR apply OR alert)',
    'subject:"business analyst" (job OR hiring OR apply OR alert)',
    'subject:"operations analyst" (job OR alert)',
    'from:jobalerts-noreply@linkedin.com',
    'from:jobalerts@linkedin.com',
    'from:noreply@glassdoor.com',
    'from:noreply@ziprecruiter.com',
    'from:noreply@builtin.com',
    'from:noreply@aquent.com',
    'from:jobnotifications',
    'subject:"job matches"',
    'subject:"recommended jobs"',
    'subject:"apply now" analyst',
]

# ── HELPERS ───────────────────────────────────────────────
def is_excluded(title):
    t = title.lower()
    return any(ex in t for ex in EXCLUDE_WORDS)

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

def fetch_linkedin_company(url):
    """Fetch company name from LinkedIn job posting via public API."""
    try:
        job_id_match = re.search(r'/jobs/view/(\d+)', url)
        if not job_id_match:
            return ''
        job_id = job_id_match.group(1)
        api_url = f'https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}'
        req = urllib.request.Request(api_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml',
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            html = r.read().decode('utf-8', errors='replace')
        co_match = re.search(r'class="topcard__org-name-link[^"]*"[^>]*>\s*([^<]+)', html)
        if not co_match:
            co_match = re.search(r'"companyName"\s*:\s*"([^"]+)"', html)
        if co_match:
            return co_match.group(1).strip()
    except Exception:
        pass
    return 

def dedup_url(j):
    url = j.get('url', '').strip().lower().rstrip('/')
    # Normalize LinkedIn URLs: comm/jobs/view/ID and jobs/view/ID are the same job
    m = re.search(r'/jobs/view/(\d+)', url)
    if m:
        return f'linkedin.com/jobs/view/{m.group(1)}'
    return url.split('?')[0]

def dedup_alt(j):
    return (j.get('company', '') + '|' + j.get('title', '')).lower().strip()

# ── GITHUB ────────────────────────────────────────────────
def read_github_file(token):
    req = urllib.request.Request(
        GITHUB_API,
        headers={'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json', 'User-Agent': 'JSA-Scraper'}
    )
    with urllib.request.urlopen(req) as r:
        meta = json.loads(r.read())
    sha = meta['sha']
    content = base64.b64decode(meta['content']).decode('utf-8')
    return json.loads(content), sha

def write_github_file(token, data, sha, message):
    content = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
    body = json.dumps({'message': message, 'content': content, 'sha': sha}).encode()
    req = urllib.request.Request(
        GITHUB_API, data=body, method='PUT',
        headers={'Authorization': f'token {token}', 'Content-Type': 'application/json',
                 'Accept': 'application/vnd.github.v3+json', 'User-Agent': 'JSA-Scraper'}
    )
    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read())
    return result['content']['sha']

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
                clean = re.sub(r'\d+\.\d+\s*[★\u2605]?\s*', '', text)
                # Extract salary if present
                salary_split = re.split(r'\s+(\$[\d,K\-]+)', clean)
                if len(salary_split) > 1:
                    salary = salary_split[1].strip()
                    clean = salary_split[0].strip()
                # Find where title starts (strip company prefix)
                for kw in ['Analyst','Engineer','Consultant','Specialist','Developer',
                           'Scientist','Associate','Data ','Business ','Financial ']:
                    ki = clean.find(kw)
                    if ki > 0:
                        clean = clean[ki:]
                        break
                # Extract trailing "City, ST" or "City ST" pattern as location
                # But don't cut into the job title itself
                loc_pattern = re.search(r'\s+([A-Za-z\s]+,\s*[A-Z]{2}|Long Island[\w\-\s]*)$', clean)
                if loc_pattern:
                    potential_loc = loc_pattern.group(1).strip()
                    # Only treat as location if it's not part of the title
                    if not any(w in potential_loc.lower() for w in ['analyst','engineer','consultant','specialist']):
                        location = potential_loc
                        clean = clean[:loc_pattern.start()].strip()
                title = clean.strip()

            elif 'builtin.com' in href:
                for kw in ['Threat ','Risk ','Fraud ','Analyst','Engineer','Consultant',
                           'Specialist','Developer','Scientist','Associate','Coordinator',
                           'Data ','Business ','Financial ','Marketing ','Sales ',
                           'Operations ','Intelligence ','Reporting ','Research ']:
                    ki = text.find(kw)
                    if ki > 0:
                        company = text[:ki].strip()
                        title = text[ki:]
                        title = re.split(r'\s+(Hybrid|Remote|In Office|USA|\$|\d{5})', title)[0].strip()
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
        if not any(p in title.lower() for p in TARGET_TITLE_PATTERNS):
            continue
        if is_excluded(title):
            continue
        # Reject confirmed out-of-area — check both location field and title
        check_text = (location + ' ' + title).lower()
        if location:
            # Location is known — check it's in target area
            if not any(t in location.lower() for t in TARGET_STATES):
                continue
        else:
            # No location field — check if title contains out-of-area city/state
            if any(marker in check_text for marker in OUT_OF_AREA_MARKERS):
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
def main():
    print(f"JSA Scraper starting — {datetime.now(timezone.utc).isoformat()}")

    github_token = os.environ.get('JSA_GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not github_token:
        raise RuntimeError('JSA_GH_TOKEN not set')

    print("Reading current jsa_jobs.json from GitHub...")
    current_data, sha = read_github_file(github_token)
    existing_jobs = current_data.get('jobs', [])
    dismissed_urls = set(current_data.get('dismissed', []))
    last_run = current_data.get('lastRun')
    print(f"Current: {len(existing_jobs)} jobs, {len(dismissed_urls)} dismissed, lastRun: {last_run}")

    existing_urls = set(dedup_url(j) for j in existing_jobs if j.get('url'))
    existing_urls.update(u.lower().rstrip('/') for u in dismissed_urls)
    existing_alts = set(dedup_alt(j) for j in existing_jobs)

    print("Connecting to Gmail...")
    gmail_service, _ = get_gmail_service()
    label_id = get_or_create_label(gmail_service, JSA_LABEL)

    if last_run:
        since = datetime.fromisoformat(last_run.replace('Z', '+00:00')) - timedelta(days=LOOKBACK_DAYS)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=30)
    after_date = since.strftime('%Y/%m/%d')
    print(f"Searching emails after: {after_date}")

    all_message_ids = {}
    for query in SEARCH_QUERIES:
        full_query = f'{query} after:{after_date} -label:{JSA_LABEL}'
        try:
            result = gmail_service.users().messages().list(
                userId='me', q=full_query, maxResults=100
            ).execute()
            messages = result.get('messages', [])
            for m in messages:
                all_message_ids[m['id']] = True
            print(f"  Query '{query[:50]}': {len(messages)} messages")
        except Exception as e:
            print(f"  Query failed '{query[:40]}': {e}")

    print(f"Total unique messages: {len(all_message_ids)}")

    new_jobs, labeled, duped = [], 0, 0

    for msg_id in all_message_ids:
        try:
            msg = gmail_service.users().messages().get(
                userId='me', id=msg_id, format='full'
            ).execute()
            headers = {h['name']: h['value'] for h in msg['payload'].get('headers', [])}
            subject = headers.get('Subject', '')
            body_html, body_text = decode_body(msg['payload'])
            jobs = extract_jobs_from_email(body_html, body_text, subject)
            print(f"  MSG {msg_id[:8]}: {subject[:45]!r} → {len(jobs)} jobs")

            for j in jobs:
                uk = dedup_url(j)
                ak = dedup_alt(j)
                if uk in existing_urls or ak in existing_alts:
                    duped += 1
                    continue
                new_jobs.append(j)
                existing_urls.add(uk)
                existing_alts.add(ak)

            thread_id = msg.get('threadId')
            if thread_id:
                gmail_service.users().threads().modify(
                    userId='me', id=thread_id,
                    body={'addLabelIds': [label_id]}
                ).execute()
                labeled += 1

        except Exception as e:
            print(f"  Error {msg_id}: {e}")
            continue

    print(f"\nResults:")
    print(f"  New jobs: {len(new_jobs)}")
    print(f"  Duplicates: {duped}")
    print(f"  Labeled: {labeled}")

    # Enrich LinkedIn jobs missing company name
    for j in new_jobs:
        if j.get('company') in ('', 'See posting') and 'linkedin.com' in j.get('url', ''):
            co = fetch_linkedin_company(j['url'])
            if co:
                j['company'] = co
                print(f"  Enriched: {j['title']} → {co}")

    if new_jobs:
        updated = {
            'jobs': new_jobs + existing_jobs,
            'outreach': current_data.get('outreach', []),
            'dismissed': list(dismissed_urls),
            'lastRun': datetime.now(timezone.utc).isoformat(),
            'savedAt': datetime.now(timezone.utc).isoformat()
        }
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        new_sha = write_github_file(github_token, updated, sha,
                                    f'JSA harvest {today}: {len(new_jobs)} new jobs')
        print(f"  Written to GitHub: {new_sha[:8]}")
        print(f"  Total jobs: {len(updated['jobs'])}")
    else:
        print("  No new jobs — file unchanged.")

    print(f"\nDone — {datetime.now(timezone.utc).isoformat()}")


if __name__ == '__main__':
    main()
