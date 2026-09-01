#!/usr/bin/env python3
"""
JSA Gmail Scraper
Reads job alert emails from Gmail, extracts listings, writes to GitHub.
Runs daily via GitHub Actions.
"""

import os
import re
import json
import base64
import quopri
import hashlib
import time
from datetime import datetime, timezone, timedelta

# Gmail API
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# GitHub API
import urllib.request
import urllib.error

# ── CONFIGURATION ─────────────────────────────────────────
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
GITHUB_REPO = 'jglenn2nd-L-ohist/Toolkit'
GITHUB_FILE = 'jsa_jobs.json'
GITHUB_API  = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}'
GITHUB_RAW  = f'https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_FILE}'
JSA_LABEL   = 'JSA-Reviewed'
LOOKBACK_DAYS = 3  # Overlap window to catch missed emails

# Job board domains for URL extraction
JOB_DOMAINS = [
    # LinkedIn — direct and email tracking
    'linkedin.com/jobs', 'linkedin.com/comm/jobs',
    'email.linkedin.com', 'lnkd.in',

    # Glassdoor — direct and email tracking
    'glassdoor.com/job-listing', 'glassdoor.com/partner/jobListing', 'glassdoor.com/partner/jobListing',
    'email.glassdoor.com', 'click.email.glassdoor.com', 'links.glassdoor.com',

    # Indeed — direct and email tracking
    'indeed.com/viewjob', 'indeed.com/rc/clk', 'indeed.com/applystart',
    'click.indeed.com', 'email.indeed.com',

    # ZipRecruiter — direct and email tracking
    'ziprecruiter.com/c/', 'ziprecruiter.com/jobs/',
    'email.ziprecruiter.com', 'click.ziprecruiter.com',

    # Builtin
    'builtin.com/job', 'builtinatlanta.com',

    # Monster
    'monster.com/job', 'jobview.monster.com',
    'email.monster.com', 'click.email.monster.com',

    # Recruiter / Staffing
    'talent.aquent.com', 'remotehunter.com',

    # ATS platforms (where jobs actually live)
    'lever.co', 'greenhouse.io', 'workday.com', 'myworkdayjobs.com',
    'icims.com', 'smartrecruiters.com', 'jobvite.com', 'taleo.net',
    'successfactors.com', 'bamboohr.com', 'breezy.hr', 'recruitee.com',

    # Generic career pages
    'careers.', '/jobs/', 'job-listing', 'job_detail', 'apply/job',
]

# Title exclusion words
EXCLUDE_WORDS = [
    'senior', 'sr.', 'sr ', 'lead', 'principal', 'staff',
    'intern', 'internship', 'co-op', 'coop',
    'behavioral health', 'substance abuse', 'mental health counselor',
    'director', 'vp ', 'vice president', 'head of', 'manager,', 'collections specialist', 'credit and collections',
    'summer 202', 'spring 202', 'early career intern'
]


# Target states (when location is known)
TARGET_STATES = [
    'georgia', ' ga', ', ga', 'atlanta',
    'florida', ' fl', ', fl', 'miami', 'orlando', 'tampa', 'jacksonville',
    'north carolina', ' nc', ', nc', 'charlotte', 'raleigh',
    'south carolina', ' sc', ', sc', 'charleston', 'columbia',
    'louisiana', ' la', ', la', 'new orleans', 'baton rouge',
    'alabama', ' al', ', al', 'birmingham', 'huntsville',
    'texas', ' tx', ', tx', 'houston', 'dallas', 'austin', 'san antonio',
    'remote', 'united states', 'us', 'anywhere'
]

SALARY_FLOOR_ANNUAL = 60000
SALARY_FLOOR_HOURLY = 29.0  # ~$60K/yr at 40hrs/52wks

def parse_salary(salary_str):
    """
    Returns annual equivalent if parseable, None if no salary info.
    Returns False if salary is below floor.
    """
    if not salary_str:
        return None  # No salary info — keep the job
    s = salary_str.lower().replace(',', '').replace('$', '').strip()
    
    # Extract numbers
    import re
    numbers = re.findall(r'\d+\.?\d*', s)
    if not numbers:
        return None
    
    # Take the higher number if range (e.g. $60K-$80K → use 60K as floor check)
    low = float(numbers[0])
    
    # Detect unit
    if 'k' in s:
        low *= 1000
    
    if '/hr' in s or 'per hr' in s or 'hour' in s:
        annual = low * 40 * 52
    else:
        annual = low
    
    if annual < SALARY_FLOOR_ANNUAL:
        return False  # Below floor — reject
    return annual  # Above floor — keep

def is_location_ok(location_str):
    """Returns True if location matches target states or is unknown/remote."""
    if not location_str:
        return True  # No location info — keep
    loc = location_str.lower()
    return any(state in loc for state in TARGET_STATES)

# Gmail search queries
SEARCH_QUERIES = [
    'subject:"job alert" analyst',
    'subject:"data analyst" OR subject:"business analyst" OR subject:"operations analyst"',
    'from:jobalerts-noreply@linkedin.com',
    'from:noreply@glassdoor.com analyst',
    'from:@indeed.com analyst',
    'from:@ziprecruiter.com analyst',
    'from:@builtin.com analyst',
    'from:@monster.com analyst',
    'from:noreply@aquent.com',
]

def is_excluded(title):
    t = title.lower()
    return any(ex in t for ex in EXCLUDE_WORDS)

def extract_urls(html, text=''):
    """Extract job-related URLs from HTML href attributes and plain text."""
    job_urls = []
    seen = set()

    # Primary: extract from HTML href attributes (catches Glassdoor/LinkedIn tracking links)
    href_matches = re.findall(r'href=["\'](https?://[^"\'\s>]+)["\'\s>]', html, re.IGNORECASE)

    # Secondary: extract from plain text
    text_matches = re.findall(r"https?://[^\s<>)\]\\]+", text)

    for url in href_matches + text_matches:
        url = url.rstrip('.,;)&')
        url = url.replace('&amp;', '&')
        if any(domain in url for domain in JOB_DOMAINS):
            base = url.split('?')[0].lower().rstrip('/')
            if base not in seen:
                seen.add(base)
                job_urls.append(url)

    return job_urls

def detect_source(url):
    if 'linkedin.com' in url: return 'LinkedIn'
    if 'glassdoor.com' in url: return 'Glassdoor'
    if 'indeed.com' in url: return 'Indeed'
    if 'ziprecruiter.com' in url: return 'ZipRecruiter'
    if 'builtin.com' in url: return 'Builtin'
    if 'dice.com' in url: return 'Dice'
    if 'aquent.com' in url: return 'Recruiter'
    if 'remotehunter.com' in url: return 'Other'
    return 'Other'

def uid():
    return hex(int(time.time() * 1000))[2:] + base64.urlsafe_b64encode(os.urandom(3)).decode()[:4]

def get_gmail_service():
    """Authenticate and return Gmail service."""
    creds = None
    token_data = os.environ.get('GMAIL_TOKEN')
    client_secret_data = os.environ.get('GMAIL_CLIENT_SECRET')

    if token_data:
        creds = Credentials.from_authorized_user_info(json.loads(token_data), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError('No valid credentials. Run auth locally first.')

    return build('gmail', 'v1', credentials=creds), creds

def get_or_create_label(service, label_name):
    """Get JSA-Reviewed label ID, create if missing."""
    labels = service.users().labels().list(userId='me').execute()
    for label in labels.get('labels', []):
        if label['name'] == label_name:
            return label['id']
    # Create it
    created = service.users().labels().create(
        userId='me',
        body={'name': label_name, 'labelListVisibility': 'labelShow', 'messageListVisibility': 'show'}
    ).execute()
    return created['id']

def decode_body(payload):
    """Recursively extract both raw HTML and plain text from email payload.
    Handles both base64 body data and quoted-printable content encoding."""
    html = ''
    text = ''
    if payload.get('body', {}).get('data'):
        try:
            raw_bytes = base64.urlsafe_b64decode(payload['body']['data'])
            # Check content-transfer-encoding header
            headers = {h['name'].lower(): h['value'] for h in payload.get('headers', [])}
            cte = headers.get('content-transfer-encoding', '').lower()
            if 'quoted-printable' in cte:
                raw = quopri.decodestring(raw_bytes).decode('utf-8', errors='replace')
            else:
                raw = raw_bytes.decode('utf-8', errors='replace')
            # Also attempt QP decode if we see =3D patterns (common in email HTML)
            if '=3D' in raw or '=2F' in raw:
                try:
                    raw = quopri.decodestring(raw_bytes).decode('utf-8', errors='replace')
                except Exception:
                    pass
            mime = payload.get('mimeType', '')
            if 'html' in mime:
                html += raw
            else:
                text += raw
        except Exception:
            pass
    for part in payload.get('parts', []):
        mime = part.get('mimeType', '')
        if mime in ('text/plain', 'text/html') or mime.startswith('multipart/'):
            h, t = decode_body(part)
            html += h
            text += t
    return html, text

def extract_jobs_from_email(body_html, body_text, subject):
    """
    Extract job listings from email body.
    For LinkedIn single-job alerts: extract the one job.
    For digest emails: extract all job URLs found.
    """
    jobs = []
    urls = extract_urls(body_html, body_text)

    if not urls:
        return jobs

    # Try to extract company/title from LinkedIn single-job alerts
    # Subject format: '"Title" at Company - ...' or 'Title at Company posted on...'
    linkedin_match = re.match(r'^["\u201c\u201d]?(.+?)["\u201c\u201d]?\s+at\s+(.+?)(?:\s*[-\u2013]|\s+posted)', subject)

    if linkedin_match and len(urls) <= 3:
        # Single job alert
        title = linkedin_match.group(1).strip().strip('"')
        company = linkedin_match.group(2).strip()
        if not is_excluded(title) and urls[0]:
            jobs.append({
                'id': uid(),
                'company': company,
                'title': title,
                'location': '',
                'salary': '',
                'url': urls[0],
                'source': detect_source(urls[0]),
                'status': 'New',
                'viewed': False,
                'dateAdded': datetime.now(timezone.utc).isoformat(),
                'notes': ''
            })
    else:
        # Digest — extract all job URLs with titles from anchor text
        for url in urls[:30]:  # cap per email
            # Method 1: Extract title from anchor text surrounding this URL
            url_fragment = url.split('?')[0][-30:]  # use end of base URL as unique fragment
            anchor_pattern = rf'<a[^>]*href=["\'][^"\']*{re.escape(url_fragment)}[^"\']*["\'][^>]*>(.*?)</a>'
            anchor_match = re.search(anchor_pattern, body_html, re.IGNORECASE | re.DOTALL)
            title = ''
            if anchor_match:
                title = re.sub(r'<[^>]+>', '', anchor_match.group(1)).strip()
                title = re.sub(r'\s+', ' ', title).strip()

            # Method 2: Extract from surrounding HTML context
            if not title or len(title) < 5:
                idx = body_html.find(url_fragment)
                if idx > -1:
                    context = body_html[max(0, idx-400):idx+300]
                    clean = re.sub(r'<[^>]+>', ' ', context)
                    clean = re.sub(r'\s+', ' ', clean).strip()
                    title_match = re.search(
                        r'([A-Z][A-Za-z\s,&\-/]{8,60}(?:Analyst|Analytics Engineer|Engineer|Specialist|Consultant|Developer|Scientist|Associate|Coordinator|Advisor))',
                        clean
                    )
                    if title_match:
                        title = title_match.group(1).strip()

            # Method 3: LinkedIn numeric ID URLs — keep URL, generic title
            if not title and 'linkedin.com/jobs/view/' in url:
                title = 'Analyst Role'

            if not title or len(title) < 5:
                continue

            if is_excluded(title):
                continue

            # Try to extract company from context
            company = 'See posting'
            idx = body_html.find(url_fragment)
            if idx > -1:
                context = body_html[max(0, idx-400):idx+400]
                clean = re.sub(r'<[^>]+>', ' ', context)
                clean = re.sub(r'\s+', ' ', clean).strip()
                # Company often appears after the title
                title_pos = clean.find(title[:20])
                if title_pos > -1:
                    after = clean[title_pos+len(title):title_pos+len(title)+100].strip()
                    company_match = re.match(r'^[·\-–—]?\s*([A-Z][A-Za-z\s&,\.]+?)\s*[·\-–—|]', after)
                    if company_match:
                        company = company_match.group(1).strip()

            jobs.append({
                'id': uid(),
                'company': company,
                'title': title,
                'location': '',
                'salary': '',
                'url': url,
                'source': detect_source(url),
                'status': 'New',
                'viewed': False,
                'dateAdded': datetime.now(timezone.utc).isoformat(),
                'notes': ''
            })

    return jobs

def read_github_file(token):
    """Read current jsa_jobs.json from GitHub, return (data, sha)."""
    req = urllib.request.Request(
        GITHUB_API,
        headers={
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'JSA-Scraper'
        }
    )
    with urllib.request.urlopen(req) as r:
        meta = json.loads(r.read())
    sha = meta['sha']
    content = base64.b64decode(meta['content']).decode('utf-8')
    data = json.loads(content)
    return data, sha

def write_github_file(token, data, sha, message):
    """Write updated data to GitHub."""
    content = base64.b64encode(json.dumps(data, indent=2).encode('utf-8')).decode('utf-8')
    body = json.dumps({'message': message, 'content': content, 'sha': sha}).encode('utf-8')
    req = urllib.request.Request(
        GITHUB_API,
        data=body,
        method='PUT',
        headers={
            'Authorization': f'token {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'JSA-Scraper'
        }
    )
    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read())
    return result['content']['sha']

def dedup_key_url(j):
    return j.get('url', '').strip().lower().rstrip('/')

def dedup_key_alt(j):
    return (j.get('company', '') + '|' + j.get('title', '')).lower().strip()

def main():
    print(f"JSA Scraper starting — {datetime.now(timezone.utc).isoformat()}")

    github_token = os.environ.get('GITHUB_TOKEN')
    if not github_token:
        raise RuntimeError('GITHUB_TOKEN environment variable not set')

    # Read current GitHub data
    print("Reading current jsa_jobs.json from GitHub...")
    current_data, sha = read_github_file(github_token)
    existing_jobs = current_data.get('jobs', [])
    dismissed_urls = set(current_data.get('dismissed', []))  # permanent blocklist
    last_run = current_data.get('lastRun')
    print(f"Current: {len(existing_jobs)} jobs, {len(dismissed_urls)} dismissed, lastRun: {last_run}")

    # Build dedup sets — include dismissed URLs so they never return
    existing_urls = set(dedup_key_url(j) for j in existing_jobs if j.get('url'))
    existing_urls.update(u.lower().rstrip('/') for u in dismissed_urls)
    existing_alts = set(dedup_key_alt(j) for j in existing_jobs)

    # Connect to Gmail
    print("Connecting to Gmail...")
    gmail_service, creds = get_gmail_service()
    label_id = get_or_create_label(gmail_service, JSA_LABEL)

    # Calculate search date
    if last_run:
        since = datetime.fromisoformat(last_run.replace('Z', '+00:00')) - timedelta(days=LOOKBACK_DAYS)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=30)
    after_date = since.strftime('%Y/%m/%d')
    print(f"Searching emails after: {after_date}")

    # Collect all message IDs across all queries
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
            print(f"Query '{query[:40]}': {len(messages)} messages")
        except Exception as e:
            print(f"Query failed: {e}")

    print(f"Total unique messages to process: {len(all_message_ids)}")

    # Process each message
    new_jobs = []
    labeled_count = 0
    excluded_count = 0
    duped_count = 0

    for msg_id in all_message_ids:
        try:
            msg = gmail_service.users().messages().get(
                userId='me', id=msg_id, format='full'
            ).execute()

            # Get subject
            headers = {h['name']: h['value'] for h in msg['payload'].get('headers', [])}
            subject = headers.get('Subject', '')

            # Decode body — get both HTML and plain text
            body_html, body_text = decode_body(msg['payload'])

            # Extract jobs
            jobs = extract_jobs_from_email(body_html, body_text, subject)

            for j in jobs:
                if is_excluded(j['title']):
                    excluded_count += 1
                    continue
                if not j.get('url'):
                    excluded_count += 1
                    continue
                # Salary floor check
                salary_check = parse_salary(j.get('salary', ''))
                if salary_check is False:
                    excluded_count += 1
                    continue
                # Location filter
                if not is_location_ok(j.get('location', '')):
                    excluded_count += 1
                    continue
                url_key = dedup_key_url(j)
                alt_key = dedup_key_alt(j)
                if url_key in existing_urls or alt_key in existing_alts:
                    duped_count += 1
                    continue
                new_jobs.append(j)
                existing_urls.add(url_key)
                existing_alts.add(alt_key)

            # Label the thread
            thread_id = msg.get('threadId')
            if thread_id:
                gmail_service.users().threads().modify(
                    userId='me',
                    id=thread_id,
                    body={'addLabelIds': [label_id]}
                ).execute()
                labeled_count += 1

        except Exception as e:
            print(f"Error processing message {msg_id}: {e}")
            continue

    print(f"\nResults:")
    print(f"  New jobs found: {len(new_jobs)}")
    print(f"  Excluded: {excluded_count}")
    print(f"  Duplicates: {duped_count}")
    print(f"  Threads labeled: {labeled_count}")

    if new_jobs:
        # Write to GitHub
        updated_data = {
            'jobs': new_jobs + existing_jobs,
            'outreach': current_data.get('outreach', []),
            'dismissed': list(dismissed_urls),
            'lastRun': datetime.now(timezone.utc).isoformat(),
            'savedAt': datetime.now(timezone.utc).isoformat()
        }
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        new_sha = write_github_file(
            github_token, updated_data, sha,
            f'JSA harvest {today}: {len(new_jobs)} new jobs'
        )
        print(f"  Written to GitHub. New SHA: {new_sha[:8]}")
        print(f"  Total jobs now: {len(updated_data['jobs'])}")
    else:
        print("  No new jobs — GitHub file unchanged.")

    print(f"\nDone — {datetime.now(timezone.utc).isoformat()}")

if __name__ == '__main__':
    main()
