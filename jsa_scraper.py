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
import urllib.parse
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
    # LinkedIn
    'linkedin.com/jobs', 'linkedin.com/comm/jobs',
    # Glassdoor
    'glassdoor.com/job-listing', 'glassdoor.com/partner/jobListing',
    'glassdoor.com/apply', 'glassdoor.com/job',
    # Indeed — direct and tracking
    'indeed.com/viewjob', 'indeed.com/rc/clk', 'indeed.com/applystart',
    'indeed.com/pagead/clk',
    # ZipRecruiter — /km/ and /ekm/ are their apply tracking links
    'ziprecruiter.com/km/', 'ziprecruiter.com/ekm/', 'ziprecruiter.com/c/',
    # Monster tracking
    'click.monster.com',
    # Builtin — decoded from awstrack
    'builtin.com/job', 'builtinatlanta.com',
    # ATS platforms
    'lever.co', 'greenhouse.io', 'workday.com', 'myworkdayjobs.com',
    'icims.com', 'smartrecruiters.com', 'jobvite.com', 'taleo.net',
    'successfactors.com', 'bamboohr.com',
    # Recruiter
    'talent.aquent.com', 'remotehunter.com',
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
    # Broad keyword searches — catch everything regardless of sender
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
    # Sender-specific (verified senders from Gmail)
    'from:jobalerts-noreply@linkedin.com',
    'from:jobalerts@linkedin.com',
    'from:noreply@glassdoor.com',
    'from:alert@glassdoor.com',
    'from:jobs@glassdoor.com',
    'from:noreply@ziprecruiter.com',
    'from:noreply@builtin.com',
    'from:noreply@aquent.com',
    # Generic job notification emails
    'from:jobnotifications',
    'subject:"job matches"',
    'subject:"recommended jobs"',
    'subject:"apply now" analyst',
]

def is_excluded(title):
    t = title.lower()
    return any(ex in t for ex in EXCLUDE_WORDS)

def extract_urls(html, text=''):
    """Extract job-related URLs from HTML href attributes and plain text."""
    job_urls = []
    seen = set()

    # Primary: extract from HTML href attributes
    href_matches = re.findall(r'href=["\'](https?://[^"\']{10,})["\'\s>]', html, re.IGNORECASE)

    # Secondary: extract from plain text
    text_matches = re.findall(r"https?://[^\s<>)\]\\]+", text)

    for url in href_matches + text_matches:
        url = url.rstrip('.,;)&').replace('&amp;', '&')

        # Decode AWS tracking URLs (Builtin uses these)
        # Format: https://xxxxx.awstrack.me/L0/https:%2F%2Fbuiltin.com%2F...
        if 'awstrack.me/L0/' in url:
            try:
                encoded_part = url.split('/L0/')[1]
                url = urllib.parse.unquote(encoded_part)
            except Exception:
                pass

        # Skip non-job URLs
        skip_patterns = ['.png', '.jpg', '.gif', '.svg', '.ico', '.woff',
                        '/assets/', '/images/', 'unsubscribe', 'optout',
                        'mailto:', 'tel:', 'facebook.com', 'twitter.com',
                        'instagram.com', 'privacy-policy', 'terms-of-service',
                        'manage-preferences', 'fonts.googleapis', 'fonts.gstatic',
                        'email-preferences', '/account/login', '/account/settings']
        if any(skip in url.lower() for skip in skip_patterns):
            continue

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
    """Extract all text content from Gmail API message payload.
    Handles multipart, base64, and quoted-printable encoding."""
    html_parts = []
    text_parts = []
    
    def walk(part):
        mime = part.get('mimeType', '')
        body = part.get('body', {})
        data = body.get('data', '')
        
        if data:
            try:
                raw_bytes = base64.urlsafe_b64decode(data + '==')  # pad for safety
                # Always try QP decode — it's safe even on non-QP content
                try:
                    decoded = quopri.decodestring(raw_bytes).decode('utf-8', errors='replace')
                except Exception:
                    decoded = raw_bytes.decode('utf-8', errors='replace')
                
                if 'html' in mime:
                    html_parts.append(decoded)
                else:
                    text_parts.append(decoded)
            except Exception:
                pass
        
        # Recurse into all parts regardless of mime type
        for subpart in part.get('parts', []):
            walk(subpart)
    
    walk(payload)
    return ' '.join(html_parts), ' '.join(text_parts)
def extract_jobs_from_email(body_html, body_text, subject):
    """Extract job listings from email body."""
    jobs = []

    JUNK_ANCHORS = {
        'apply','view job','view jobs','view all jobs','view jobs in last 7 days',
        '1-click apply','here','apply now','create','easy apply',
        'show me more →','show me more','privacy policy','contact us',
        'sign in','sign up','your profile','unsubscribe.','unsubscribe',
        'manage job alerts','manage alerts','view all','see all jobs',
        'manage my alerts','settings','terms','help center',
        'download app','get the app','view on website','share your feedback',
        'get more recommendations','job preferences','view job',
    }

    JOB_WORDS = ['analyst', 'analytics', 'engineer', 'consultant', 'specialist',
                 'scientist', 'coordinator', 'advisor', 'intelligence',
                 'reporting', 'pricing', 'revenue', 'strategy analyst',
                 'data analyst', 'business analyst', 'financial analyst',
                 'operations analyst', 'marketing analyst', 'risk analyst',
                 'fraud analyst', 'compliance analyst', 'actuarial',
                 'business intelligence', 'data quality', 'procurement analyst',
                 'logistics analyst', 'planning analyst', 'research analyst',
                 'systems analyst', 'insights analyst', 'performance analyst']

    SKIP_URL = ['.png', '.jpg', '.gif', '.svg', '.ico', '.woff',
                '/assets/', '/images/', 'unsubscribe', 'optout',
                'mailto:', 'tel:', 'facebook.com', 'twitter.com',
                'instagram.com', 'privacy-policy', 'terms-of-service',
                'manage-preferences', 'fonts.googleapis', 'fonts.gstatic',
                'email-preferences', '/account/login', '/account/settings',
                'form.jotform', 'feedback', 'survey']

    # Step 1: Extract ALL (url, raw_inner_text) pairs from anchors
    anchor_pairs = []
    for m in re.finditer(r'<a\s([^>]+)>(.*?)</a>', body_html, re.DOTALL):
        attrs, inner = m.group(1), m.group(2)
        href_m = re.search(r'href=["\']([^"\'>]+)["\']', attrs)
        if not href_m:
            continue
        href = href_m.group(1).rstrip('.,;)&').replace('&amp;', '&')
        
        # Decode Builtin AWS tracking URLs
        if 'awstrack.me/L0/' in href:
            try:
                href = urllib.parse.unquote(href.split('/L0/')[1])
            except Exception:
                pass

        if any(s in href.lower() for s in SKIP_URL):
            continue

        # Clean inner text
        text = re.sub(r'<[^>]+>', ' ', inner)
        text = text.replace('&amp;', '&').replace('&nbsp;', ' ').replace('&#39;', "\'")                   .replace('&#x2F;', '/').replace('&lt;', '<').replace('&gt;', '>')                   .replace('\u2605', '').replace('\u2192', '')
        text = re.sub(r'\s+', ' ', text).strip()

        anchor_pairs.append((href, text))

    # Step 2: For each job domain URL, find the best title
    seen_urls = set()
    seen_alts = set()

    for href, text in anchor_pairs:
        # Is this a job URL?
        if not any(d in href for d in JOB_DOMAINS):
            continue
        
        base = href.split('?')[0].lower().rstrip('/')
        if base in seen_urls:
            continue

        title = ''
        company = ''
        salary = ''
        location = ''

        # Try the anchor text first
        if text and len(text) > 4 and text.lower().strip('.→') not in JUNK_ANCHORS:
            
            # Glassdoor: "Company 4.0 ★ Title Location $Salary"
            if 'glassdoor.com' in href:
                # Remove company name and rating: "Company Name 4.0 ★ Title..."
                # Remove rating numbers and star symbol
                clean = re.sub(r'\d+\.\d+\s*[★\u2605]?\s*', '', text)
                # If still has company prefix, find where title starts
                for kw in ['Analyst','Engineer','Consultant','Specialist','Developer',
                           'Scientist','Associate','Coordinator','Manager','Data ','Business ',
                           'Financial ','Marketing ','Sales ','Research ','Strategy ']:
                    ki = clean.find(kw)
                    if ki > 0:
                        clean = clean[ki:]
                        break
                # Split on location/salary (2+ spaces or city names)
                parts = re.split(r'\s{2,}|\s+(?=Remote|Atlanta|Georgia|Florida|Texas|\$[0-9])', clean)
                title = parts[0].strip()
                if len(parts) > 1: location = parts[1].strip()
                if len(parts) > 2: salary = parts[2].strip()

            # Builtin: "CompanyNameJob Title..."
            elif 'builtin.com' in href:
                # Builtin packs "CompanyTitle Location Salary" 
                # Company name is CamelCase with no space before title keyword
                JOB_STARTS = ['Threat ','Risk ','Fraud ','Analyst','Engineer','Consultant',
                              'Specialist','Developer','Scientist','Associate','Coordinator',
                              'Manager','Advisor','Data ','Business ','Financial ',
                              'Marketing ','Sales ','Operations ','Intelligence ',
                              'Reporting ','Pricing ','Research ','Strategy ']
                matched = False
                for kw in JOB_STARTS:
                    ki = text.find(kw)
                    if ki > 0:
                        company = text[:ki].strip()
                        title = text[ki:].strip()
                        # Remove location/salary from end of title
                        title = re.split(r'\s+(Hybrid|Remote|In Office|USA|\$|\d{5})', title)[0].strip()
                        matched = True
                        break
                if not matched:
                    title = text

            # ZipRecruiter, Monster, LinkedIn, Indeed: title is directly in anchor
            else:
                title = text

        # If no title from anchor, try Monster-specific: find next title anchor
        if not title or len(title) < 4:
            if 'click.monster.com' in href or 'monster.com' in href:
                # Monster puts title in a nearby anchor — scan next 5 anchor pairs
                href_idx = next((i for i,(h,_) in enumerate(anchor_pairs) 
                                 if h == href), None)
                if href_idx is not None:
                    for _h, next_text in anchor_pairs[href_idx+1:href_idx+6]:
                        nt = next_text.strip()
                        if (len(nt) > 8 
                            and nt.lower() not in JUNK_ANCHORS
                            and any(w in nt.lower() for w in JOB_WORDS)):
                            title = nt
                            break

        # If still no title, try nearby HTML context
        if not title or len(title) < 4:
            idx = body_html.find(href[:50])
            if idx > -1:
                ctx = body_html[max(0,idx-400):idx+200]
                ctx_clean = re.sub(r'<[^>]+>', ' ', ctx).replace('&nbsp;', ' ')
                ctx_clean = re.sub(r'\s+', ' ', ctx_clean).strip()
                tm = re.search(
                    r'([A-Z][A-Za-z\s&,\-/]{8,60}(?:Analyst|Engineer|Consultant|Specialist|Developer|Scientist|Associate|Coordinator|Operations|Intelligence|Research|Advisor))',
                    ctx_clean
                )
                if tm:
                    title = tm.group(1).strip()

        # Validate title
        if not title or len(title) < 4:
            continue
        if title.lower().strip('.→') in JUNK_ANCHORS:
            continue
        if not any(w in title.lower() for w in JOB_WORDS):
            continue
        if is_excluded(title):
            continue

        # Dedup by title+company
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
