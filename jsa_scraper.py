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
    'linkedin.com/jobs', 'indeed.com/viewjob', 'indeed.com/rc/clk',
    'glassdoor.com/job-listing', 'glassdoor.com/partner/jobListing',
    'ziprecruiter.com', 'builtin.com/job', 'dice.com',
    'lever.co', 'greenhouse.io', 'workday.com', 'myworkdayjobs.com',
    'icims.com', 'smartrecruiters.com', 'jobvite.com', 'taleo.net',
    'talent.aquent.com', 'remotehunter.com', 'careers.', 'jobs.'
]

# Title exclusion words
EXCLUDE_WORDS = [
    'senior', 'sr.', 'sr ', 'lead', 'principal', 'staff',
    'intern', 'internship', 'co-op', 'coop',
    'behavioral health', 'substance abuse', 'mental health counselor',
    'director', 'vp ', 'vice president', 'head of', 'manager,',
    'summer 202', 'spring 202', 'early career intern'
]

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

def extract_urls(text):
    """Extract job-related URLs from email body text."""
    url_pattern = r'https?://[^\s<>"\')\]\\]+'
    all_urls = re.findall(url_pattern, text)
    job_urls = []
    for url in all_urls:
        url = url.rstrip('.,;)')
        if any(domain in url for domain in JOB_DOMAINS):
            job_urls.append(url)
    return list(dict.fromkeys(job_urls))  # deduplicate preserving order

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
    """Recursively extract text from email payload."""
    text = ''
    if payload.get('body', {}).get('data'):
        try:
            text += base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='replace')
        except Exception:
            pass
    for part in payload.get('parts', []):
        mime = part.get('mimeType', '')
        if mime in ('text/plain', 'text/html') or mime.startswith('multipart/'):
            text += decode_body(part)
    return text

def extract_jobs_from_email(body_text, subject):
    """
    Extract job listings from email body.
    For LinkedIn single-job alerts: extract the one job.
    For digest emails: extract all job URLs found.
    """
    jobs = []
    urls = extract_urls(body_text)

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
        # Digest — extract all job URLs
        # Try to find title/company pairs near each URL in the HTML
        for url in urls[:20]:  # cap per email
            # Try to find nearby text context
            idx = body_text.find(url)
            context = body_text[max(0, idx-300):idx+100] if idx > -1 else ''

            # Strip HTML tags for context parsing
            clean_context = re.sub(r'<[^>]+>', ' ', context)
            clean_context = re.sub(r'\s+', ' ', clean_context).strip()

            # Try to extract title from context
            title = ''
            company = ''

            # Pattern: look for text before the URL that looks like a job title
            title_match = re.search(r'([A-Z][A-Za-z\s,&\-/]{10,60}(?:Analyst|Engineer|Specialist|Consultant|Manager|Developer|Scientist|Associate|Coordinator|Advisor))', clean_context)
            if title_match:
                title = title_match.group(1).strip()

            if not title:
                # Fall back to URL slug for LinkedIn
                if 'linkedin.com/jobs/view/' in url:
                    title = 'Analyst Role'  # will be visible via URL
                else:
                    continue  # skip if we can't identify title

            if is_excluded(title):
                continue

            jobs.append({
                'id': uid(),
                'company': company or 'See posting',
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
    last_run = current_data.get('lastRun')
    print(f"Current: {len(existing_jobs)} jobs, lastRun: {last_run}")

    # Build dedup sets
    existing_urls = set(dedup_key_url(j) for j in existing_jobs if j.get('url'))
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

            # Decode body
            body = decode_body(msg['payload'])

            # Extract jobs
            jobs = extract_jobs_from_email(body, subject)

            for j in jobs:
                if is_excluded(j['title']):
                    excluded_count += 1
                    continue
                if not j.get('url'):
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
