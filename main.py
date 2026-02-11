import csv
import os
import re
import time
from dataclasses import dataclass
from typing import Iterable, List, Set

from dotenv import load_dotenv
import requests

load_dotenv()
from bs4 import BeautifulSoup

SEARCH_URL = (
    "https://www.linkedin.com/jobs/search/"
    "?keywords=engineer"
    "&location=United States"
    "&f_TPR=r86400"   # posted in last 24 hours
    "&f_WT=2"         # remote only
    "&sortBy=DD"      # sort by date posted (most recent first)
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


@dataclass
class JobResult:
    url: str
    company_name: str = ""


def normalize_job_url(url: str) -> str:
    if url.startswith("/"):
        url = "https://www.linkedin.com" + url
    base = url.split("?")[0].split("#")[0]
    if "/jobs/view/" not in base:
        return base
    match = re.search(r"/jobs/view/([^/]+)/?$", base)
    if not match:
        return base
    segment = match.group(1).rstrip("/")
    if segment.isdigit():
        job_id = segment
    else:
        # slug form: something-like-this-4370870454 → job_id is 4370870454
        parts = segment.split("-")
        job_id = parts[-1] if parts and parts[-1].isdigit() else segment
    return f"https://www.linkedin.com/jobs/view/{job_id}"


def fetch_page(url: str, session: requests.Session) -> str:
    """Fetch a single page HTML."""
    resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_job_links_from_search(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: Set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/jobs/view/" in href:
            # Normalize to short form: https://www.linkedin.com/jobs/view/{job_id}
            if href.startswith("/"):
                href = "https://www.linkedin.com" + href
            href_base = href.split("?")[0].split("#")[0]
            if "/jobs/view/" in href_base:
                links.add(normalize_job_url(href_base))

    return sorted(links)


def is_first_party_job(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")

    # 1. Exclude jobs that explicitly say "via <external source>" near the top.
    # Look for "via " text inside the job header.
    header = soup.find("div", attrs={"data-test-job-header": True}) or soup.find(
        "div", class_="top-card-layout__entity-info"
    )
    if header:
        text = header.get_text(separator=" ", strip=True).lower()
        if " via " in text:
            return False

    # 2. Look at apply button/link text – exclude only known third‑party job boards.
    # "Apply on company website" etc. are first‑party; "Apply on Indeed" is not.
    THIRD_PARTY_KEYWORDS = ("indeed", "monster", "glassdoor", "ziprecruiter")
    apply_buttons = soup.find_all("button") + soup.find_all("a")
    for el in apply_buttons:
        label = el.get_text(separator=" ", strip=True).lower()
        if "apply on " in label and any(k in label for k in THIRD_PARTY_KEYWORDS):
            return False

    # If we didn't detect any external "via" source or third‑party apply target, treat as first‑party.
    return True


EMPLOYEE_SIZE_MIN = 1
EMPLOYEE_SIZE_MAX = 50


def parse_company_profile_url_from_job_page(html: str) -> str:
    """
    Extract the company profile page URL from a job detail page (the company link
    the user clicks to go to the company's LinkedIn page).
    Returns empty string if not found.
    """
    soup = BeautifulSoup(html, "html.parser")
    header = soup.find("div", attrs={"data-test-job-header": True}) or soup.find(
        "div", class_="top-card-layout__entity-info"
    )
    if not header:
        return ""
    for a in header.find_all("a", href=True):
        href = a.get("href", "").strip()
        if "/company/" not in href:
            continue
        if href.startswith("/"):
            href = "https://www.linkedin.com" + href
        # Strip query and fragment for canonical company page
        base = href.split("?")[0].split("#")[0]
        if base:
            return base
    return ""


def _parse_employee_range_from_text(text: str) -> bool:
    in_range_patterns = [
        r"\b1\s*-\s*10\s+employees?\b",
        r"\b11\s*-\s*50\s+employees?\b",
        r"\b1\s+employee\b",
        r"\b2\s*-\s*10\s+employees?\b",
    ]
    for pat in in_range_patterns:
        if re.search(pat, text, re.IGNORECASE):
            return True
    out_of_range_patterns = [
        r"\b51\s*-\s*200\s+employees?\b",
        r"\b201\s*-\s*500\s+employees?\b",
        r"\b501\s*-\s*1000\s+employees?\b",
        r"\b1001\s*-\s*5000\s+employees?\b",
        r"\b5001\s*-\s*10000\s+employees?\b",
        r"\b10001\s*\+\s*employees?\b",
        r"\b(?:5[1-9]|[6-9]\d|\d{3,})\s+employees?\b",
    ]
    for pat in out_of_range_patterns:
        if re.search(pat, text, re.IGNORECASE):
            return False
    return False


def is_company_size_1_to_50(job_page_html: str, session: requests.Session) -> bool:
    """
    Navigate to the company profile page (via the company link on the job page),
    locate the employee size field there, and return True only if the company
    has between 1 and 50 employees (inclusive).
    """
    company_url = parse_company_profile_url_from_job_page(job_page_html)
    if not company_url:
        return False
    try:
        company_html = fetch_page(company_url, session)
        time.sleep(1.0)  # be gentle after extra request to company page
    except requests.RequestException:
        return False
    soup = BeautifulSoup(company_html, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    return _parse_employee_range_from_text(text)


def parse_company_name_from_job_page(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    header = soup.find("div", attrs={"data-test-job-header": True}) or soup.find(
        "div", class_="top-card-layout__entity-info"
    )
    if not header:
        return ""
    # Prefer: <a href=".../company/...">Company Name</a>
    for a in header.find_all("a", href=True):
        href = a.get("href", "")
        if "/company/" in href:
            name = a.get_text(separator=" ", strip=True)
            if name and len(name) < 200:  # sanity: company name not a blob of text
                return name
    return ""


def filter_first_party_jobs(
    job_urls: Iterable[str], session: requests.Session, delay_seconds: float = 2.0
) -> List[JobResult]:
    results: List[JobResult] = []
    for url in job_urls:
        try:
            html = fetch_page(url, session)
            if is_first_party_job(html) and is_company_size_1_to_50(html, session):
                company_name = parse_company_name_from_job_page(html)
                results.append(JobResult(url=url, company_name=company_name))
        except requests.RequestException as exc:
            print(f"Failed to fetch job detail {url}: {exc}")

        time.sleep(delay_seconds)

    return results


def load_existing_job_urls(csv_path: str) -> Set[str]:
    urls: Set[str] = set()
    try:
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("job_url"):
                    raw = row["job_url"].strip()
                    urls.add(normalize_job_url(raw) if "/jobs/view/" in raw else raw)
    except FileNotFoundError:
        pass
    return urls


def append_jobs_to_csv(jobs: Iterable[JobResult], csv_path: str) -> None:
    file_exists = os.path.isfile(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            # New files: write header in the order company, job_url
            writer.writerow(["company", "job_url"])
        for job in jobs:
            # Write rows as: company, job_url
            company = getattr(job, "company_name", "") or ""
            writer.writerow([company, job.url])


def collect_recent_remote_engineer_jobs(limit_pages: int = 1) -> List[JobResult]:
    session = requests.Session()
    all_job_urls: Set[str] = set()

    for page in range(limit_pages):        
        start = page * 25  # LinkedIn typically uses 25 jobs per page
        url = f"{SEARCH_URL}&start={start}"
        print(f"Fetching search page: {url}")
        try:
            html = fetch_page(url, session)
        except requests.RequestException as exc:
            print(f"Failed to fetch search page {url}: {exc}")
            break

        # html_path = f"search_page_{page}.html"
        # with open(html_path, "w", encoding="utf-8") as f:
        #     f.write(html)
        # print(f"Saved HTML to {html_path}")

        page_links = parse_job_links_from_search(html)
        if not page_links:
            # No more results
            break

        all_job_urls.update(page_links)
        time.sleep(2.0)

    print(f"Found {len(all_job_urls)} unique job detail URLs. Filtering first‑party and company size 1–50...")
    first_party_jobs = filter_first_party_jobs(sorted(all_job_urls), session)
    print(f"Retained {len(first_party_jobs)} first‑party job(s) with company size 1–50.")

    return first_party_jobs


CSV_PATH = "data.csv"
INTERVAL_SECONDS = 3 * 60  # Run job collection every 5 minutes

SLACK_CHANNEL_ID =os.environ.get("SLACK_CHANNEL_ID")
SLACK_API_URL = "https://slack.com/api/chat.postMessage"
# Set SLACK_BOT_TOKEN in the environment to your Slack bot token (Bot User OAuth Token).


def send_job_to_slack(
    job: "JobResult",
    channel_id: str,
    token: str,
) -> bool:
    """
    Post the job URL to the given Slack channel using the bot token.
    Returns True if the message was posted successfully, False otherwise.
    """
    if not token or not channel_id:
        return False
    company = getattr(job, "company_name", "") or "Company"
    text = f"{company}: {job.url}"
    try:
        resp = requests.post(
            SLACK_API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"channel": channel_id, "text": text},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"Slack API error: {data.get('error', 'unknown')}")
            return False
        return True
    except requests.RequestException as exc:
        print(f"Slack request failed: {exc}")
        return False


def run_once() -> None:
    """Execute one full job collection cycle: load existing, collect, dedupe, append."""
    existing_urls = load_existing_job_urls(CSV_PATH)
    print(f"Existing dataset: {len(existing_urls)} job URL(s).")

    jobs = collect_recent_remote_engineer_jobs(limit_pages=1)

    new_jobs = [j for j in jobs if j.url not in existing_urls]
    duplicate_count = len(jobs) - len(new_jobs)
    if duplicate_count:
        print(f"Skipped {duplicate_count} listing(s) already in dataset.")

    slack_token = (os.environ.get("SLACK_BOT_TOKEN") or "").strip()
    if not slack_token and new_jobs:
        print("SLACK_BOT_TOKEN not set; skipping Slack. Set it to post new jobs to Slack.")

    jobs_posted_to_slack: List[JobResult] = []
    for job in new_jobs:
        if slack_token:
            if send_job_to_slack(job, SLACK_CHANNEL_ID, slack_token):
                jobs_posted_to_slack.append(job)
            else:
                print(f"Slack post failed for {job.url}; not saving to CSV.")
        else:
            jobs_posted_to_slack.append(job)

    if jobs_posted_to_slack:
        append_jobs_to_csv(jobs_posted_to_slack, CSV_PATH)
        print(f"Posted {len(jobs_posted_to_slack)} to Slack and appended to {CSV_PATH}.")
    elif new_jobs:
        print("No jobs were posted to Slack; none appended to CSV.")
    else:
        print("No new listings to add.")

    total = len(existing_urls) + len(jobs_posted_to_slack)
    print(f"CSV total: {total} unique job URL(s).")


def main() -> None:
    """Run job collection every 5 minutes indefinitely (Ctrl+C to stop)."""
    print(f"Starting job collector. Running every {INTERVAL_SECONDS // 60} minutes. Press Ctrl+C to stop.\n")
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            print("\nStopped by user.")
            raise
        print(f"\nNext run in {INTERVAL_SECONDS // 60} minutes...\n")
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

