"""searcher.py
Lightweight search fallback that scrapes DuckDuckGo HTML results and homepages
to heuristically infer company name and sector for a domain.

This module prefers requests + BeautifulSoup but falls back to urllib and
simple regex/html parsing if those libraries are not available.
Enhanced with comprehensive error recovery and monitoring.
"""
import os
import re
import time
import csv
import sqlite3
from threading import Lock
import asyncio
import logging

# Import error recovery system
try:
    from error_recovery import (
        with_error_recovery, RetryConfig, CircuitBreakerConfig,
        get_domain_circuit_breaker, metrics_collector, logger
    )
    HAS_ERROR_RECOVERY = True
except ImportError:
    # Fallback if error_recovery module not available
    HAS_ERROR_RECOVERY = False
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

try:
    import aiohttp
    import aiosqlite
    HAS_ASYNC = True
except Exception:
    HAS_ASYNC = False

# Load configurable sector rules from Dataset/sector_rules.json if present
import json
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_SECTOR_RULES_PATH = os.path.join(BASE_DIR, 'Dataset', 'sector_rules.json')
_SECTOR_RULES = None
if os.path.exists(_SECTOR_RULES_PATH):
    try:
        with open(_SECTOR_RULES_PATH, 'r', encoding='utf-8') as f:
            _SECTOR_RULES = json.load(f)
    except Exception:
        _SECTOR_RULES = None
from urllib.parse import quote_plus, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_CACHE_FILE = os.path.join(BASE_DIR, "Dataset", "search_cache.csv")
# SQLite cache (domain -> company, sector)
DB_CACHE_FILE = os.path.join(BASE_DIR, "Dataset", "search_cache.db")

# in-memory per-host last request timestamps (not persisted)
_host_last_request = {}
_host_lock = Lock()


def _ensure_db():
    os.makedirs(os.path.dirname(DB_CACHE_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_CACHE_FILE)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS cache (
            domain TEXT PRIMARY KEY,
            company TEXT,
            sector TEXT,
            last_seen INTEGER
        )
    ''')
    conn.commit()
    conn.close()

# Try to import requests and bs4, otherwise mark as unavailable
try:
    import requests
    from bs4 import BeautifulSoup
    HAS_REQUESTS = True
except Exception:
    HAS_REQUESTS = False
    from urllib import request as urllib_request


def _save_cache_row(domain, company, sector):
    # write to sqlite cache
    _ensure_db()
    conn = sqlite3.connect(DB_CACHE_FILE)
    cur = conn.cursor()
    cur.execute('REPLACE INTO cache(domain, company, sector, last_seen) VALUES (?, ?, ?, ?)',
                (domain, company, sector, int(time.time())))
    conn.commit()
    conn.close()
    # also append to CSV cache for compatibility/backups, but avoid duplicates
    os.makedirs(os.path.dirname(CSV_CACHE_FILE), exist_ok=True)
    # only append to CSV if domain not present in sqlite DB (prevents duplicates)
    existing = _get_cache(domain)
    if existing is None:
        write_header = not os.path.exists(CSV_CACHE_FILE)
        with open(CSV_CACHE_FILE, "a", newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(["domain", "company", "sector"])
            writer.writerow([domain, company, sector])


def _read_cache():
    # read from sqlite cache if present, else csv cache
    _ensure_db()
    conn = sqlite3.connect(DB_CACHE_FILE)
    cur = conn.cursor()
    cur.execute('SELECT domain, company, sector FROM cache')
    rows = cur.fetchall()
    conn.close()
    cache = {r[0]: (r[1] or 'Unknown', r[2] or 'Unknown') for r in rows}
    # fallback: if DB empty and CSV exists, read CSV
    if not cache and os.path.exists(CSV_CACHE_FILE):
        with open(CSV_CACHE_FILE, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for r in reader:
                cache[r['domain']] = (r.get('company', 'Unknown'), r.get('sector', 'Unknown'))
    return cache


def _get_cache(domain):
    _ensure_db()
    conn = sqlite3.connect(DB_CACHE_FILE)
    cur = conn.cursor()
    cur.execute('SELECT company, sector FROM cache WHERE domain = ?', (domain,))
    r = cur.fetchone()
    conn.close()
    if r:
        return (r[0] or 'Unknown', r[1] or 'Unknown')
    return None


def import_csv_to_db(csv_path=None):
    """Import rows from the CSV cache into the SQLite DB. Returns number imported."""
    csv_path = csv_path or CSV_CACHE_FILE
    if not os.path.exists(csv_path):
        return 0
    _ensure_db()
    imported = 0
    conn = sqlite3.connect(DB_CACHE_FILE)
    cur = conn.cursor()
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for r in reader:
            domain = r.get('domain')
            company = r.get('company') or 'Unknown'
            sector = r.get('sector') or 'Unknown'
            try:
                cur.execute('REPLACE INTO cache(domain, company, sector, last_seen) VALUES (?, ?, ?, ?)',
                            (domain, company, sector, int(time.time())))
                imported += 1
            except Exception:
                continue
    conn.commit()
    conn.close()
    return imported


def clean_csv_cache(csv_path=None, backup=True):
    """Remove duplicate rows from CSV cache, keeping first occurrence for each domain.
    If backup=True, create a timestamped backup of the original CSV.
    Returns number of rows written (excluding header).
    """
    csv_path = csv_path or CSV_CACHE_FILE
    if not os.path.exists(csv_path):
        return 0
    if backup:
        import shutil, datetime
        bak = csv_path + ".bak." + datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        shutil.copy2(csv_path, bak)
    seen = set()
    rows = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for r in reader:
            d = r.get('domain')
            if not d:
                continue
            if d in seen:
                continue
            seen.add(d)
            rows.append((d, r.get('company', 'Unknown'), r.get('sector', 'Unknown')))

    # rewrite CSV with header
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['domain', 'company', 'sector'])
        for r in rows:
            writer.writerow(r)
    return len(rows)


def save_cache(domain, company, sector):
    """Public wrapper to save a cache entry (to sqlite and CSV backup)."""
    return _save_cache_row(domain, company, sector)


def _set_host_wait(host, min_delay):
    # ensure at least min_delay seconds between requests to the same host
    with _host_lock:
        last = _host_last_request.get(host)
        now = time.time()
        if last is not None:
            wait = min_delay - (now - last)
            if wait > 0:
                logger.debug(f"Rate limiting: waiting {wait:.2f}s for {host}")
                time.sleep(wait)
        _host_last_request[host] = time.time()


# ----------------------
# Async helpers (aiohttp + aiosqlite)
# ----------------------
@with_error_recovery(
    retry_config=RetryConfig(max_attempts=2, base_delay=1.0, max_delay=5.0),
    track_metrics=True
) if HAS_ERROR_RECOVERY else lambda f: f
async def async_fetch_homepage(session, domain):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; searcher/1.0)"}
    candidates = [f"https://{domain}", f"http://{domain}"]
    
    for url in candidates:
        try:
            logger.debug(f"Attempting to fetch homepage: {url}")
            async with session.get(url, headers=headers, timeout=8) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    logger.info(f"Successfully fetched homepage for {domain}")
                    return str(resp.url), text
                else:
                    logger.warning(f"HTTP {resp.status} for {url}")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout fetching {url}")
        except Exception as e:
            logger.warning(f"Error fetching {url}: {e}")
            continue
    
    logger.error(f"Failed to fetch homepage for {domain}")
    return None, None


@with_error_recovery(
    retry_config=RetryConfig(max_attempts=3, base_delay=0.5, max_delay=10.0),
    track_metrics=True
) if HAS_ERROR_RECOVERY else lambda f: f
async def async_search_domain(domain, pause=0.0, min_delay=0.0, session=None, db_path=None):
    logger.info(f"Starting async search for domain: {domain}")
    
    # try sqlite cache first (aiosqlite)
    if HAS_ASYNC and db_path:
        try:
            async with aiosqlite.connect(db_path) as db:
                cur = await db.execute('SELECT company, sector FROM cache WHERE domain = ?', (domain,))
                row = await cur.fetchone()
                await cur.close()
                if row:
                    logger.debug(f"Cache hit for {domain}: {row[0]}, {row[1]}")
                    if HAS_ERROR_RECOVERY:
                        metrics_collector.record_cache_hit()
                    return (row[0] or 'Unknown', row[1] or 'Unknown')
        except Exception as e:
            logger.warning(f"Cache lookup failed for {domain}: {e}")
    
    if HAS_ERROR_RECOVERY:
        metrics_collector.record_cache_miss()

    # duckduckgo search (sync fallback)
    logger.debug(f"Performing DuckDuckGo search for {domain}")
    results = duckduckgo_search(domain, max_results=3)
    candidate_name = None
    candidate_text = ''
    homepage_url = None
    homepage_html = None

    for title, url in results:
        parsed = urlparse(url)
        netloc = parsed.netloc
        if domain in netloc:
            candidate_name = title
            if session:
                homepage_url, homepage_html = await async_fetch_homepage(session, domain)
            break

    if not homepage_html and session:
        homepage_url, homepage_html = await async_fetch_homepage(session, domain)

    if homepage_html:
        candidate = extract_company_from_html(homepage_html, homepage_url)
        if candidate:
            candidate_name = candidate
        candidate_text = homepage_html

    if not candidate_name and results:
        candidate_name = results[0][0]

    sector = infer_sector_from_text(candidate_text or (candidate_name or ''))
    
    # If sector is still Unknown, try WikiData lookup
    if sector == 'Unknown' and candidate_name:
        try:
            wiki_sector = wikidata_lookup_company(candidate_name)
            if wiki_sector and wiki_sector != 'Unknown':
                sector = wiki_sector
        except Exception:
            pass  # Continue with Unknown
    
    company = candidate_name or domain.split('.')[0].capitalize()

    # write to sqlite cache asynchronously if available
    if HAS_ASYNC and db_path:
        try:
            async with aiosqlite.connect(db_path) as db:
                await db.execute('REPLACE INTO cache(domain, company, sector, last_seen) VALUES (?, ?, ?, ?)',
                                 (domain, company, sector, int(time.time())))
                await db.commit()
        except Exception:
            pass

    if pause:
        await asyncio.sleep(pause)
    return company, sector


async def async_resolve_domains(domains, workers=10, min_delay=0.2):
    if not HAS_ASYNC:
        raise RuntimeError('Async dependencies (aiohttp/aiosqlite) not installed')
    results = {}
    sem = asyncio.Semaphore(workers)
    async with aiohttp.ClientSession() as session:
        async def worker(domain):
            async with sem:
                return await async_search_domain(domain, pause=0.0, min_delay=min_delay, session=session, db_path=DB_CACHE_FILE)

        tasks = [asyncio.create_task(worker(d)) for d in domains]
        for d, t in zip(domains, tasks):
            try:
                res = await t
            except Exception:
                res = ('Unknown', 'Unknown')
            results[d] = res
    return results


def duckduckgo_search(query, max_results=5):
    """Enhanced DuckDuckGo search with better resilience and user agents."""
    results = []
    q = quote_plus(query)
    
    # Try multiple search endpoints and user agents
    search_urls = [
        f"https://duckduckgo.com/html/?q={q}",
        f"https://lite.duckduckgo.com/lite/?q={q}",
    ]
    
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0"
    ]
    
    import random
    
    for url in search_urls:
        for ua in user_agents[:2]:  # Try first 2 user agents per URL
            headers = {
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }
            
            try:
                if HAS_REQUESTS:
                    resp = requests.get(url, headers=headers, timeout=15)
                    resp.raise_for_status()
                    html = resp.text
                    
                    # Check if we got blocked
                    if "blocked" in html.lower() or "captcha" in html.lower():
                        logger.warning(f"Search blocked for {query}, trying different approach")
                        time.sleep(random.uniform(1, 3))
                        continue
                    
                else:
                    req = urllib_request.Request(url, headers=headers)
                    with urllib_request.urlopen(req, timeout=15) as r:
                        html = r.read().decode('utf-8', errors='ignore')
                
                # Parse results and return if successful
                if html:
                    parsed_results = parse_search_results(html)
                    if parsed_results:
                        return parsed_results[:max_results]
                        
            except Exception as e:
                logger.debug(f"Search attempt failed with {ua[:20]}...: {e}")
                time.sleep(random.uniform(0.5, 1.5))
                continue
    
    logger.warning(f"All search attempts failed for query: {query}")
    return results


def parse_search_results(html):
    """Parse search results from HTML."""
    results = []

    # Parse HTML to find result links
    if HAS_REQUESTS:
        soup = BeautifulSoup(html, 'html.parser')
        # Try multiple selectors for different DuckDuckGo layouts
        selectors = ['a.result__a', 'a[class*="result"]', '.result-title a', 'h3 a']
        
        for selector in selectors:
            links = soup.select(selector)
            if links:
                for a in links[:10]:  # Limit to prevent too many results
                    title = a.get_text(strip=True)
                    href = a.get('href')
                    if href and title and len(title) > 2:
                        results.append((title, href))
                break  # Use first successful selector
    else:
        # Enhanced regex fallback for different link patterns
        patterns = [
            r'<a[^>]+class="result__a"[^>]+href="(?P<h>[^\"]+)"[^>]*>(?P<t>.*?)</a>',
            r'<a[^>]+href="(?P<h>[^\"]+)"[^>]*class="[^"]*result[^"]*"[^>]*>(?P<t>.*?)</a>',
            r'<h3[^>]*><a[^>]+href="(?P<h>[^\"]+)"[^>]*>(?P<t>.*?)</a></h3>'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, html, re.I | re.S)
            if matches:
                for href, title in matches[:10]:
                    clean_title = re.sub('<[^<]+?>', '', title).strip()
                    if clean_title and len(clean_title) > 2:
                        results.append((clean_title, href))
                break  # Use first successful pattern

    return results


@with_error_recovery(
    retry_config=RetryConfig(max_attempts=2, base_delay=1.0, max_delay=5.0),
    track_metrics=True
) if HAS_ERROR_RECOVERY else lambda f: f
def fetch_homepage(domain):
    """Enhanced homepage fetching with better headers and fallback strategies."""
    import random
    
    # Realistic browser headers
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document"
    }
    
    # Try multiple URL variations
    candidates = [
        f"https://www.{domain}",
        f"https://{domain}",
        f"http://www.{domain}",
        f"http://{domain}"
    ]
    
    for url in candidates:
        try:
            logger.debug(f"Attempting to fetch homepage: {url}")
            
            if HAS_REQUESTS:
                # Add small random delay to appear more human-like
                time.sleep(random.uniform(0.1, 0.3))
                
                resp = requests.get(
                    url, 
                    headers=headers, 
                    timeout=12,
                    allow_redirects=True,
                    verify=False  # Skip SSL verification for some difficult sites
                )
                
                # Check if response is valid
                if resp.status_code == 200 and resp.text:
                    # Check if we got blocked or got a parking page
                    text_lower = resp.text.lower()
                    blocked_indicators = [
                        'blocked', 'captcha', 'cloudflare', 'access denied',
                        'domain parking', 'this domain may be for sale',
                        'coming soon', '403 forbidden', '404 not found'
                    ]
                    
                    if not any(indicator in text_lower for indicator in blocked_indicators):
                        logger.info(f"Successfully fetched homepage for {domain}")
                        return resp.url, resp.text
                    else:
                        logger.warning(f"Got blocked or parking page for {url}")
                        continue
                else:
                    logger.warning(f"Invalid response for {url}: {resp.status_code}")
                    
            else:
                # Fallback urllib method
                req = urllib_request.Request(url, headers=headers)
                with urllib_request.urlopen(req, timeout=12) as r:
                    html = r.read().decode('utf-8', errors='ignore')
                    if html and len(html) > 100:  # Basic validity check
                        logger.info(f"Successfully fetched homepage for {domain}")
                        return r.geturl(), html
                        
        except requests.exceptions.SSLError:
            logger.debug(f"SSL error for {url}, trying without verification")
            continue
        except requests.exceptions.RequestException as e:
            logger.warning(f"Request error for {url}: {e}")
            continue
        except Exception as e:
            logger.warning(f"Error fetching {url}: {e}")
            continue
    
    logger.error(f"Failed to fetch homepage for {domain}")
    return None, None


def extract_company_from_html(html, url=None):
    """Enhanced company name extraction from HTML with better parsing."""
    name_candidates = []
    
    if html and HAS_REQUESTS:
        soup = BeautifulSoup(html, 'html.parser')
        
        # Priority 1: Meta tags (most reliable)
        meta_props = [
            'og:site_name', 'application-name', 'og:title', 
            'twitter:title', 'description', 'og:description'
        ]
        for prop in meta_props:
            tag = soup.find('meta', property=prop) or soup.find('meta', attrs={'name': prop})
            if tag and tag.get('content'):
                content = tag.get('content').strip()
                if len(content) > 2:
                    name_candidates.append(content)
        
        # Priority 2: Title tag
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
            if title:
                name_candidates.append(title)
        
        # Priority 3: Company-specific selectors
        company_selectors = [
            '.company-name', '.brand-name', '.logo-text', 
            '[class*="company"]', '[class*="brand"]',
            'h1.title', 'h1.name', '.site-title'
        ]
        for selector in company_selectors:
            elements = soup.select(selector)
            for elem in elements[:2]:  # Limit to first 2 matches
                text = elem.get_text(strip=True)
                if text and len(text) > 1:
                    name_candidates.append(text)
        
        # Priority 4: Header tags
        for tag in ['h1', 'h2']:
            headers = soup.find_all(tag, limit=3)
            for header in headers:
                text = header.get_text(strip=True)
                if text and len(text) > 1:
                    name_candidates.append(text)
        
        # Priority 5: Navigation links (often contain company name)
        nav_selectors = ['nav a', '.navbar a', '.menu a', 'header a']
        for selector in nav_selectors:
            links = soup.select(selector)
            for link in links[:3]:
                text = link.get_text(strip=True)
                if text and len(text) > 1 and 'home' in text.lower():
                    name_candidates.append(text)
    
    elif html:
        # Fallback parsing without BeautifulSoup
        patterns = [
            r'<meta[^>]+(?:property="og:site_name"|name="application-name")[^>]+content="([^"]+)"',
            r'<title[^>]*>([^<]+)</title>',
            r'<h1[^>]*>([^<]+)</h1>'
        ]
        for pattern in patterns:
            matches = re.findall(pattern, html, re.I | re.S)
            for match in matches:
                name_candidates.append(match.strip())

    # Clean and score candidates
    cleaned_candidates = []
    for candidate in name_candidates:
        cleaned = clean_company_candidate(candidate)
        if cleaned and is_valid_company_name(cleaned):
            cleaned_candidates.append(cleaned)
    
    # Return best candidate based on scoring
    if cleaned_candidates:
        return score_company_names(cleaned_candidates)
    
    # Ultimate fallback: derive from URL
    if url:
        parsed = urlparse(url)
        host = parsed.netloc.replace('www.', '')
        parts = host.split('.')
        if len(parts) >= 2:
            return parts[-2].capitalize()

    return None


def clean_company_candidate(candidate):
    """Clean up a company name candidate."""
    if not candidate:
        return None
    
    # Remove common website suffixes and prefixes
    patterns_to_remove = [
        r'\s*-\s*(Official Site|Home|Website|Company|Inc|LLC|Corp|Ltd).*$',
        r'^(Welcome to|About|The)\s+',
        r'\s*\|\s*.*$',  # Everything after pipe
        r'\s*-\s*.*$',   # Everything after dash (less aggressive)
        r'\s*&[^;]+;',   # HTML entities
        r'^\s*Home\s*$', # Just "Home"
        r'^\s*Index\s*$' # Just "Index"
    ]
    
    for pattern in patterns_to_remove:
        candidate = re.sub(pattern, '', candidate, flags=re.I)
    
    # Clean up whitespace and return
    return ' '.join(candidate.split()).strip()


def is_valid_company_name(name):
    """Check if a cleaned name looks like a valid company name."""
    if not name or len(name) < 2:
        return False
    
    # Reject common non-company terms
    invalid_terms = [
        '404', 'error', 'not found', 'loading', 'please wait',
        'home', 'index', 'welcome', 'login', 'sign in', 'menu',
        'search', 'contact', 'about', 'services', 'products'
    ]
    
    name_lower = name.lower()
    for term in invalid_terms:
        if name_lower == term or (len(name_lower) < 10 and term in name_lower):
            return False
    
    # Must contain at least one letter
    if not re.search(r'[a-zA-Z]', name):
        return False
    
    return True


def score_company_names(candidates):
    """Score company name candidates and return the best one."""
    if not candidates:
        return None
    
    if len(candidates) == 1:
        return candidates[0]
    
    scored = []
    for candidate in candidates:
        score = 0
        
        # Prefer shorter, cleaner names
        if len(candidate) < 30:
            score += 2
        if len(candidate) < 15:
            score += 1
        
        # Prefer names without common website words
        website_words = ['website', 'site', 'official', 'home', 'welcome']
        if not any(word in candidate.lower() for word in website_words):
            score += 3
        
        # Prefer capitalized names
        if candidate[0].isupper():
            score += 1
        
        # Prefer names with proper capitalization
        if candidate.istitle() or any(c.isupper() for c in candidate[1:]):
            score += 1
        
        scored.append((score, candidate))
    
    # Return highest scored candidate
    scored.sort(reverse=True, key=lambda x: x[0])
    return scored[0][1]


def infer_sector_from_text(text, sector_keywords=None):
    """Enhanced sector inference with comprehensive keyword mapping and smart analysis."""
    if not text:
        return 'Unknown'
    
    text = text.lower()
    
    # First, check for direct company name mappings (expanded)
    company_sectors = {
        # Technology & Software
        'gmail': 'Technology', 'google': 'Technology', 'apple': 'Technology', 
        'microsoft': 'Technology', 'facebook': 'Technology', 'meta': 'Technology',
        'instagram': 'Technology', 'twitter': 'Technology', 'linkedin': 'Technology',
        'github': 'Technology', 'adobe': 'Technology', 'oracle': 'Technology',
        'salesforce': 'Technology', 'zoom': 'Technology', 'slack': 'Technology',
        'dropbox': 'Technology', 'netflix': 'Media & Entertainment', 'spotify': 'Media & Entertainment',
        'intel': 'Technology', 'nvidia': 'Technology', 'amd': 'Technology',
        'ibm': 'Technology', 'hp': 'Technology', 'dell': 'Technology',
        
        # E-commerce & Retail
        'amazon': 'E-commerce', 'shopify': 'E-commerce', 'ebay': 'E-commerce',
        'alibaba': 'E-commerce', 'walmart': 'Retail', 'target': 'Retail',
        
        # Finance & Banking
        'paypal': 'Finance', 'visa': 'Finance', 'mastercard': 'Finance',
        'jpmorgan': 'Finance', 'goldman': 'Finance', 'citi': 'Finance',
        
        # Transportation & Automotive
        'uber': 'Transportation', 'tesla': 'Automotive', 'ford': 'Automotive',
        'toyota': 'Automotive', 'bmw': 'Automotive', 'mercedes': 'Automotive',
        
        # Hospitality & Travel
        'airbnb': 'Hospitality', 'booking': 'Hospitality', 'expedia': 'Travel',
        'marriott': 'Hospitality', 'hilton': 'Hospitality',
        
        # Healthcare & Pharmaceutical
        'pfizer': 'Healthcare', 'johnson': 'Healthcare', 'merck': 'Healthcare',
        'novartis': 'Healthcare', 'roche': 'Healthcare',
    }
    
    # Check if any company name is directly mentioned
    for company, sector in company_sectors.items():
        if company in text:
            return sector
    
    if sector_keywords is None:
        # Comprehensive keyword mapping with priority scoring
        sector_keywords = {
            # Technology (highest priority for tech terms)
            'software development': 'Technology',
            'web development': 'Technology', 
            'mobile app': 'Technology',
            'artificial intelligence': 'Technology',
            'machine learning': 'Technology',
            'data science': 'Technology',
            'cloud computing': 'Technology',
            'cybersecurity': 'Technology',
            'blockchain': 'Technology',
            'cryptocurrency': 'Technology',
            'saas': 'Technology',
            'tech': 'Technology',
            'software': 'Technology',
            'technology': 'Technology',
            'information technology': 'Technology',
            'it services': 'Technology',
            'internet': 'Technology',
            'cloud': 'Technology',
            'digital': 'Technology',
            'platform': 'Technology',
            'app': 'Technology',
            'api': 'Technology',
            'programming': 'Technology',
            'coding': 'Technology',
            'development': 'Technology',
            
            # Finance & Banking
            'financial services': 'Finance',
            'investment banking': 'Finance',
            'wealth management': 'Finance',
            'asset management': 'Finance',
            'private equity': 'Finance',
            'venture capital': 'Finance',
            'cryptocurrency': 'Finance',
            'fintech': 'Finance',
            'banking': 'Finance',
            'bank': 'Finance',
            'finance': 'Finance',
            'insurance': 'Finance',
            'investment': 'Finance',
            'fund': 'Finance',
            'capital': 'Finance',
            'trading': 'Finance',
            'securities': 'Finance',
            'credit': 'Finance',
            'loan': 'Finance',
            'mortgage': 'Finance',
            'payment': 'Finance',
            
            # Healthcare & Medical
            'healthcare services': 'Healthcare',
            'medical device': 'Healthcare',
            'pharmaceutical': 'Healthcare',
            'biotechnology': 'Healthcare',
            'health insurance': 'Healthcare',
            'telemedicine': 'Healthcare',
            'clinical': 'Healthcare',
            'hospital': 'Healthcare',
            'clinic': 'Healthcare',
            'health': 'Healthcare',
            'medical': 'Healthcare',
            'pharma': 'Healthcare',
            'biotech': 'Healthcare',
            'medicine': 'Healthcare',
            'patient': 'Healthcare',
            'therapy': 'Healthcare',
            'diagnostic': 'Healthcare',
            
            # E-commerce & Retail
            'e-commerce platform': 'E-commerce',
            'online retail': 'E-commerce',
            'marketplace': 'E-commerce',
            'e-commerce': 'E-commerce',
            'ecommerce': 'E-commerce',
            'online store': 'E-commerce',
            'retail': 'Retail',
            'store': 'Retail',
            'shop': 'Retail',
            'shopping': 'Retail',
            'merchandise': 'Retail',
            'consumer goods': 'Retail',
            
            # Manufacturing & Industrial
            'manufacturing': 'Manufacturing',
            'production': 'Manufacturing',
            'factory': 'Manufacturing',
            'industrial': 'Manufacturing',
            'automotive': 'Manufacturing',
            'aerospace': 'Manufacturing',
            'chemical': 'Manufacturing',
            'materials': 'Manufacturing',
            'assembly': 'Manufacturing',
            'supply chain': 'Manufacturing',
            
            # Consulting & Professional Services
            'management consulting': 'Consulting',
            'business consulting': 'Consulting',
            'strategy consulting': 'Consulting',
            'it consulting': 'Consulting',
            'consulting': 'Consulting',
            'advisory': 'Consulting',
            'professional services': 'Consulting',
            'legal services': 'Legal Services',
            'law firm': 'Legal Services',
            'attorney': 'Legal Services',
            'lawyer': 'Legal Services',
            
            # Media & Entertainment
            'media production': 'Media & Entertainment',
            'content creation': 'Media & Entertainment',
            'streaming': 'Media & Entertainment',
            'broadcasting': 'Media & Entertainment',
            'entertainment': 'Media & Entertainment',
            'gaming': 'Media & Entertainment',
            'publishing': 'Media & Entertainment',
            'media': 'Media & Entertainment',
            'news': 'Media & Entertainment',
            'television': 'Media & Entertainment',
            'radio': 'Media & Entertainment',
            'film': 'Media & Entertainment',
            'video': 'Media & Entertainment',
            
            # Education & Training
            'educational technology': 'Education',
            'online learning': 'Education',
            'training': 'Education',
            'education': 'Education',
            'university': 'Education',
            'school': 'Education',
            'learning': 'Education',
            'course': 'Education',
            'academic': 'Education',
            
            # Real Estate
            'real estate': 'Real Estate',
            'property management': 'Real Estate',
            'construction': 'Real Estate',
            'architecture': 'Real Estate',
            'property': 'Real Estate',
            'building': 'Real Estate',
            
            # Transportation & Logistics
            'logistics': 'Transportation & Logistics',
            'supply chain': 'Transportation & Logistics',
            'shipping': 'Transportation & Logistics',
            'delivery': 'Transportation & Logistics',
            'transport': 'Transportation & Logistics',
            'freight': 'Transportation & Logistics',
            'warehouse': 'Transportation & Logistics',
            
            # Hospitality & Travel
            'hospitality': 'Hospitality & Travel',
            'hotel': 'Hospitality & Travel',
            'travel': 'Hospitality & Travel',
            'tourism': 'Hospitality & Travel',
            'restaurant': 'Food & Beverage',
            'food service': 'Food & Beverage',
            'catering': 'Food & Beverage',
            
            # Energy & Utilities
            'renewable energy': 'Energy',
            'solar energy': 'Energy',
            'wind energy': 'Energy',
            'oil and gas': 'Energy',
            'utilities': 'Energy',
            'energy': 'Energy',
            'power': 'Energy',
            'electricity': 'Energy',
            'oil': 'Energy',
            'gas': 'Energy',
            'utility': 'Energy',
            
            # Telecommunications
            'telecommunications': 'Telecommunications',
            'telecom': 'Telecommunications',
            'mobile': 'Telecommunications',
            'wireless': 'Telecommunications',
            'network': 'Telecommunications',
            'internet service': 'Telecommunications',
            
            # Nonprofit & Government
            'non-profit': 'Non-profit',
            'nonprofit': 'Non-profit',
            'charity': 'Non-profit',
            'foundation': 'Non-profit',
            'government': 'Government',
            'public sector': 'Government',
            
            # Agriculture & Food
            'agriculture': 'Agriculture',
            'farming': 'Agriculture',
            'food production': 'Agriculture',
            'agribusiness': 'Agriculture',
        }
    
    # Multi-pass scoring system for better accuracy
    sector_scores = {}
    
    # Score based on exact phrase matches (higher weight)
    for keyword, sector in sector_keywords.items():
        if keyword in text:
            if sector not in sector_scores:
                sector_scores[sector] = 0
            # Weight longer phrases more heavily
            weight = len(keyword.split()) * 2
            sector_scores[sector] += weight
    
    # Additional scoring for partial word matches
    words = text.split()
    for word in words:
        for keyword, sector in sector_keywords.items():
            if word in keyword.split():
                if sector not in sector_scores:
                    sector_scores[sector] = 0
                sector_scores[sector] += 0.5
    
    # Return the sector with highest score
    if sector_scores:
        best_sector = max(sector_scores, key=sector_scores.get)
        # Only return if confidence is high enough
        if sector_scores[best_sector] >= 1.0:
            return best_sector
    
    return 'Unknown'


def wikidata_lookup_company(company_name):
    """Try to find a company's sector using Wikidata search and basic property inspection.
    This is a lightweight helper that queries Wikidata's search API. It is best-effort.
    Returns a sector string or None.
    """
    try:
        url = 'https://www.wikidata.org/w/api.php'
        params = {
            'action': 'wbsearchentities',
            'search': company_name,
            'language': 'en',
            'format': 'json',
            'type': 'item',
            'limit': 1
        }
        import requests
        resp = requests.get(url, params=params, timeout=6)
        resp.raise_for_status()
        data = resp.json()
        if 'search' in data and len(data['search']) > 0:
            qid = data['search'][0]['id']
            # fetch claims
            props = {'action': 'wbgetentities', 'ids': qid, 'format': 'json', 'props': 'claims|labels'}
            r2 = requests.get(url, params=props, timeout=6)
            r2.raise_for_status()
            ent = r2.json().get('entities', {}).get(qid, {})
            claims = ent.get('claims', {})
            # common properties that can hint at sector/industry: P31 (instance of), P452 (industry)
            if 'P452' in claims:
                for c in claims['P452']:
                    mainsnak = c.get('mainsnak', {})
                    datavalue = mainsnak.get('datavalue', {})
                    if datavalue:
                        # get the label of the industry
                        vid = datavalue.get('value', {}).get('id')
                        if vid:
                            lbl = requests.get(url, params={'action': 'wbgetentities', 'ids': vid, 'format': 'json', 'props': 'labels', 'languages': 'en'}, timeout=6)
                            lbl.raise_for_status()
                            lblj = lbl.json()
                            label = lblj.get('entities', {}).get(vid, {}).get('labels', {}).get('en', {}).get('value')
                            if label:
                                return label
            if 'P31' in claims:
                for c in claims['P31']:
                    mainsnak = c.get('mainsnak', {})
                    datavalue = mainsnak.get('datavalue', {})
                    if datavalue:
                        vid = datavalue.get('value', {}).get('id')
                        if vid:
                            lbl = requests.get(url, params={'action': 'wbgetentities', 'ids': vid, 'format': 'json', 'props': 'labels', 'languages': 'en'}, timeout=6)
                            lbl.raise_for_status()
                            lblj = lbl.json()
                            label = lblj.get('entities', {}).get(vid, {}).get('labels', {}).get('en', {}).get('value')
                            if label:
                                return label
    except Exception:
        return None
    return None


@with_error_recovery(
    retry_config=RetryConfig(max_attempts=3, base_delay=1.0, max_delay=15.0),
    track_metrics=True
) if HAS_ERROR_RECOVERY else lambda f: f
def search_domain(domain, pause=1.0, min_delay=0.0):
    """Enhanced domain search with multiple information sources and better sector detection."""
    logger.info(f"Starting enhanced search for domain: {domain}")
    
    # check sqlite cache first
    c = _get_cache(domain)
    if c and c[1] != 'Unknown':  # Only use cache if sector is known
        logger.debug(f"Cache hit for {domain}: {c[0]}, {c[1]}")
        if HAS_ERROR_RECOVERY:
            metrics_collector.record_cache_hit()
        return c
    
    if HAS_ERROR_RECOVERY:
        metrics_collector.record_cache_miss()

    # Aggressive search strategy with multiple queries for 100% success rate
    company_name = domain.split('.')[0]
    search_queries = [
        f"{domain} company",
        f"{domain} business", 
        f"{domain} corporation",
        f"site:{domain} about us",
        f"site:{domain} services", 
        f"site:{domain} company profile",
        f"{domain.replace('.', ' ')} company sector industry",
        f"{company_name} company {domain}",
        f"{company_name} business sector industry",
        f"{domain} what is company",
        f"{domain} business type sector",
        f'"{company_name}" company website',
        f'"{domain}" official website',
        f"{company_name} inc corp ltd company"
    ]
    
    candidate_name = None
    candidate_text = ''
    homepage_url = None
    homepage_html = None
    all_search_text = ''

    # Try multiple search queries aggressively for maximum information
    for query in search_queries[:4]:  # Increased to 4 queries for better results
        try:
            results = duckduckgo_search(query, max_results=5)
            
            for title, url in results:
                parsed = urlparse(url)
                netloc = parsed.netloc.replace('www.', '')
                
                # Collect all relevant text
                all_search_text += f" {title} "
                
                # Prefer results that point to the domain
                if domain in netloc or netloc in domain:
                    if not candidate_name:
                        candidate_name = title
                    
                    # Get homepage if we haven't already
                    if not homepage_html:
                        host = netloc.replace('www.', '')
                        _set_host_wait(host, min_delay)
                        homepage_url, homepage_html = fetch_homepage(domain)
                        if homepage_html:
                            candidate_text += homepage_html
                            break
            
            # Small delay between searches
            time.sleep(0.5)
        except Exception as e:
            logger.warning(f"Search query failed for {query}: {e}")
            continue

    # Extract company name from homepage if available
    if homepage_html:
        extracted_company = extract_company_from_html(homepage_html, homepage_url)
        if extracted_company:
            candidate_name = extracted_company
        candidate_text += homepage_html

    # Combine all available text for sector analysis
    combined_text = f"{candidate_text} {all_search_text} {candidate_name or ''}"
    
    # Enhanced sector inference
    sector = infer_sector_from_text(combined_text)
    
    # If sector is still Unknown, try additional methods
    if sector == 'Unknown':
        # Try domain-specific searches
        sector_queries = [
            f"{domain} industry sector",
            f"{candidate_name or domain} what business type"
        ]
        
        for query in sector_queries[:1]:  # Try one additional query
            try:
                results = duckduckgo_search(query, max_results=3)
                sector_text = ' '.join([title for title, _ in results])
                potential_sector = infer_sector_from_text(sector_text)
                if potential_sector != 'Unknown':
                    sector = potential_sector
                    break
                time.sleep(0.3)
            except Exception:
                continue
    
    # WikiData lookup as fallback
    if sector == 'Unknown' and candidate_name:
        try:
            wiki_sector = wikidata_lookup_company(candidate_name)
            if wiki_sector and wiki_sector != 'Unknown':
                sector = normalize_sector_name(wiki_sector)
        except Exception:
            pass
    
    # Final fallback: intelligent guessing based on domain name
    if sector == 'Unknown':
        sector = guess_sector_from_domain(domain)
    
    # Clean up company name
    company = clean_company_name(candidate_name) or domain.split('.')[0].capitalize()

    # Cache and return (always cache, even if Unknown)
    _save_cache_row(domain, company, sector)
    time.sleep(pause)
    return company, sector


def clean_company_name(name):
    """Clean and normalize company name."""
    if not name:
        return None
    
    # Remove common suffixes and prefixes
    name = re.sub(r'\s*-\s*(Official Site|Home|Website|Company).*$', '', name, flags=re.I)
    name = re.sub(r'^(Welcome to|About)\s+', '', name, flags=re.I)
    name = re.sub(r'\s*\|\s*.*$', '', name)  # Remove everything after |
    
    # Clean up extra whitespace
    name = ' '.join(name.split())
    
    return name.strip() if name.strip() else None


def normalize_sector_name(sector):
    """Normalize sector names to standard categories."""
    if not sector:
        return 'Unknown'
    
    sector = sector.lower().strip()
    
    # Mapping of variations to standard names
    sector_mapping = {
        'information technology': 'Technology',
        'software company': 'Technology',
        'technology company': 'Technology',
        'internet company': 'Technology',
        'financial services': 'Finance',
        'banking': 'Finance',
        'investment firm': 'Finance',
        'healthcare company': 'Healthcare',
        'pharmaceutical company': 'Healthcare',
        'biotechnology company': 'Healthcare',
        'retail company': 'Retail',
        'e-commerce company': 'E-commerce',
        'manufacturing company': 'Manufacturing',
        'consulting firm': 'Consulting',
        'media company': 'Media & Entertainment',
        'entertainment company': 'Media & Entertainment',
    }
    
    for key, standard in sector_mapping.items():
        if key in sector:
            return standard
    
    # Capitalize first letter of each word
    return ' '.join(word.capitalize() for word in sector.split())


def guess_sector_from_domain(domain):
    """Make comprehensive educated guesses about sector based on domain name patterns."""
    domain_lower = domain.lower()
    
    # Technology indicators (expanded)
    tech_keywords = ['tech', 'software', 'app', 'web', 'digital', 'cloud', 'data', 'ai', 'cyber', 
                    'system', 'platform', 'analytics', 'computing', 'innovation', 'lab', 'dev', 
                    'code', 'programming', 'database', 'network', 'internet', 'mobile', 'saas']
    if any(word in domain_lower for word in tech_keywords):
        return 'Technology'
    
    # Finance indicators (expanded)
    finance_keywords = ['bank', 'finance', 'invest', 'capital', 'fund', 'pay', 'credit', 'loan',
                       'insurance', 'asset', 'wealth', 'trading', 'financial', 'money', 'payment',
                       'mortgage', 'savings', 'investment', 'portfolio', 'fintech']
    if any(word in domain_lower for word in finance_keywords):
        return 'Finance'
    
    # Healthcare indicators (expanded)
    health_keywords = ['health', 'medical', 'pharma', 'bio', 'clinic', 'care', 'hospital',
                      'medicine', 'therapy', 'dental', 'wellness', 'fitness', 'healthcare',
                      'pharmaceutical', 'biotech', 'medtech', 'diagnostic', 'treatment']
    if any(word in domain_lower for word in health_keywords):
        return 'Healthcare'
    
    # Retail/E-commerce indicators (expanded)
    retail_keywords = ['shop', 'store', 'market', 'buy', 'sell', 'commerce', 'retail', 'fashion',
                      'clothing', 'shoes', 'jewelry', 'grocery', 'food', 'restaurant', 'cafe',
                      'ecommerce', 'marketplace', 'shopping', 'outlet', 'boutique']
    if any(word in domain_lower for word in retail_keywords):
        return 'Retail'
    
    # Media indicators (expanded)
    media_keywords = ['media', 'news', 'tv', 'radio', 'video', 'stream', 'broadcast', 'entertainment',
                     'film', 'music', 'publishing', 'content', 'streaming', 'production', 'studio',
                     'gaming', 'game', 'podcast', 'blog', 'magazine', 'newspaper']
    if any(word in domain_lower for word in media_keywords):
        return 'Media'
    
    # Manufacturing indicators (expanded)
    manufacturing_keywords = ['manufacturing', 'industrial', 'factory', 'production', 'auto', 'motor',
                             'mechanical', 'engineering', 'construction', 'materials', 'chemical',
                             'steel', 'metal', 'machinery', 'equipment', 'automotive', 'aerospace']
    if any(word in domain_lower for word in manufacturing_keywords):
        return 'Manufacturing'
    
    # Energy indicators
    energy_keywords = ['energy', 'oil', 'gas', 'petroleum', 'renewable', 'solar', 'wind', 'power',
                      'electric', 'utility', 'nuclear', 'coal', 'fuel', 'battery', 'grid']
    if any(word in domain_lower for word in energy_keywords):
        return 'Energy'
    
    # Transportation indicators
    transport_keywords = ['transport', 'logistics', 'shipping', 'delivery', 'airline', 'aviation',
                         'rail', 'truck', 'freight', 'cargo', 'supply', 'chain', 'warehouse',
                         'distribution', 'courier', 'express']
    if any(word in domain_lower for word in transport_keywords):
        return 'Transportation'
    
    # Real Estate indicators
    realestate_keywords = ['real', 'estate', 'property', 'housing', 'rental', 'mortgage',
                          'construction', 'development', 'builder', 'architect', 'realty']
    if any(word in domain_lower for word in realestate_keywords):
        return 'Real Estate'
    
    # Education indicators
    education_keywords = ['education', 'school', 'university', 'college', 'learning', 'training',
                         'academic', 'research', 'institute', 'educational', 'teaching', 'course']
    if any(word in domain_lower for word in education_keywords):
        return 'Education'
    
    # Consulting indicators (expanded)
    consulting_keywords = ['consult', 'advisory', 'strategy', 'solutions', 'management', 'business',
                          'professional', 'services', 'consulting', 'advisor', 'expert', 'guidance']
    if any(word in domain_lower for word in consulting_keywords):
        return 'Consulting'
    
    # Government/Non-profit indicators
    govt_keywords = ['gov', 'government', 'public', 'municipal', 'federal', 'state', 'city',
                    'nonprofit', 'foundation', 'charity', 'organization', 'association']
    if any(word in domain_lower for word in govt_keywords):
        return 'Government'
    
    # Agriculture indicators  
    agriculture_keywords = ['farm', 'agriculture', 'agricultural', 'crop', 'livestock', 'dairy',
                           'organic', 'seed', 'fertilizer', 'irrigation', 'harvest']
    if any(word in domain_lower for word in agriculture_keywords):
        return 'Agriculture'
    
    # Legal indicators
    legal_keywords = ['law', 'legal', 'attorney', 'lawyer', 'court', 'justice', 'litigation',
                     'patent', 'trademark', 'compliance', 'regulatory']
    if any(word in domain_lower for word in legal_keywords):
        return 'Legal'
    
    # Communications/Telecom indicators
    telecom_keywords = ['telecom', 'communication', 'wireless', 'cellular', 'broadband', 
                       'internet', 'network', 'phone', 'mobile', 'satellite', 'fiber']
    if any(word in domain_lower for word in telecom_keywords):
        return 'Telecommunications'
    
    # Hospitality/Travel indicators
    hospitality_keywords = ['hotel', 'travel', 'tourism', 'hospitality', 'resort', 'vacation',
                           'booking', 'airline', 'cruise', 'restaurant', 'catering']
    if any(word in domain_lower for word in hospitality_keywords):
        return 'Hospitality'
    
    # Default fallback based on common TLDs and patterns
    if domain_lower.endswith(('.edu', '.org')):
        return 'Education'
    elif domain_lower.endswith('.gov'):
        return 'Government'
    elif any(tld in domain_lower for tld in ['.tech', '.app', '.dev', '.io']):
        return 'Technology'
    elif any(word in domain_lower for word in ['inc', 'corp', 'company', 'group', 'ltd', 'llc']):
        return 'Business Services'
    
    # Final fallback - more neutral default
    return 'Business Services'
