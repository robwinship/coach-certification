import json
import os
import re
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path
from typing import Dict, List, Sequence
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

CERTIFIED_URL = "https://www.registeroba.ca/certified-coaches"
IN_PROGRESS_URL = "https://www.registeroba.ca/certification-inprogress-by-local"
COACH_STATUS_URL = "https://www.registeroba.ca/coach-certification-status"
COACH_STATUS_BY_BUCKET_URLS = {
    "A-D": "https://www.registeroba.ca/certification-in-progress-a-d",
    "E-J": "https://www.registeroba.ca/in-progress-by-coach-e-j",
    "K-O": "https://www.registeroba.ca/in-progress-k-o",
    "P-Z": "https://www.registeroba.ca/in-progress-by-coach-p-z",
}
STATUS_PATH = Path("docs/status.json")
SUMMARY_PATH = Path("docs/current-summary.html")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

MISSING_COURSE_MIN_COVERAGE = 0.20

# NonCertifiedCoaches Wix collection constants (discovered via network inspection).
_NONCERTIFIED_APP_ID = "3c6e5891-d24d-4c77-9b02-532114068664"
_NONCERTIFIED_COLLECTION = "NonCertifiedCoaches"
# Fields in the collection that are metadata, not course completion status.
# NOTE: classification1 is Safe Sport (a real course) — do NOT add it here.
# practiceEvaluation1/11/111 are Wix-generated duplicate column keys — the website
# shows only one Practice Evaluation column (the base practiceEvaluation field).
# The duplicates store "No" for coaches where they are inapplicable instead of "NR",
# causing false positives, so they are excluded here.
_NON_COURSE_FIELDS = frozenset({
    "_id", "_owner", "_createdDate", "_updatedDate",
    "title", "nccp", "position", "sort", "club", "classification",
    "practiceEvaluation1", "practiceEvaluation11", "practiceEvaluation111",
})

# Maps raw Wix API field names to human-readable display names.
# Wix auto-suffixes duplicate column labels (e.g. the second "Classification" column
# becomes "classification1" in the API — which is Safe Sport).
_COURSE_FIELD_DISPLAY_NAMES: Dict[str, str] = {
    "hitting":                               "Hitting",
    "infielding":                            "Infielding",
    "outfielding":                           "Outfielding",
    "baseRunning":                           "Base Running",
    "pitchingCatching":                      "Pitching & Catching",
    "absolutes":                             "Absolutes",
    "planning":                              "Planning",
    "strategies":                            "Strategies",
    "skills":                                "Skills",
    "coachInitiationInSport":                "Coach Initiation in Sport",
    "coachInitiationInBaseballFundamentals": "Fundamentals of Coaching Baseball",
    "makeEthicalDecisions":                  "Make Ethical Decisions",
    "teachingLearning":                      "Teaching & Learning",
    "practiceEvaluation":                    "Practice Evaluation",
    "corePortfolioEvaluation":               "Core Portfolio Evaluation",
    "gameEvaluation":                        "Game Evaluation",
    "portfolioTasks":                        "Portfolio Tasks",
    "videoPackage":                          "11U Video Package",
    "13UVideoPackage":                       "13U Video Package",
    "15UVideoPackage":                       "15U Video Package",
    "16VideoPackage":                        "16+ Video Package",
    "classification1":                       "Safe Sport",
}

MISSING_COURSE_NEGATIVE_VALUES = {
    "no",
    "pending",
    "not started",
    "not-started",
    "in progress",
    "incomplete",
    "required",
}
DEFAULT_INT_ENV: Dict[str, int] = {
    "SCRAPE_GOTO_TIMEOUT_MS": 60000,
    "SCRAPE_NETWORK_IDLE_TIMEOUT_MS": 30000,
    "SCRAPE_SELECTOR_TIMEOUT_MS": 15000,
    "COACH_STATUS_PAGE_GOTO_TIMEOUT_MS": 60000,
    "COACH_STATUS_PAGE_NETWORK_IDLE_TIMEOUT_MS": 30000,
    "COACH_STATUS_PAGE_COMBO_TIMEOUT_MS": 15000,
    "MISSING_COURSE_BUCKET_SETTLE_MS": 5000,
    "MISSING_COURSE_COMBO_TIMEOUT_MS": 10000,
    "MISSING_COURSE_OPTION_TIMEOUT_MS": 5000,
    "MISSING_COURSE_LOOKUP_TIMEOUT_MS": 10000,
    "MISSING_COURSE_SELECTION_SETTLE_MS": 500,
    "MISSING_COURSE_LOOKUP_RETRIES": 2,
    "MISSING_COURSE_RETRY_BACKOFF_MS": 600,
}
LAST_MISSING_COURSE_DIAGNOSTICS: Dict[str, object] = {}


@dataclass
class CoachRow:
    name: str
    registration_id: str
    level: str
    position: str
    association: str
    source_url: str
    missing_courses: List[str] = field(default_factory=list)
    missing_courses_available: bool = False
    missing_courses_reason: str = ""

    def key(self) -> str:
        return "|".join(
            [
                normalize(self.name),
                normalize(self.registration_id),
                normalize(self.level),
                normalize(self.position),
                normalize(self.association),
            ]
        )


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def coach_dropdown_label(row: CoachRow) -> str:
    return f"{row.name} ({row.level})"


def coach_role_label(row: CoachRow) -> str:
    return clean_text(f"{row.level} {row.position}")


def bucket_for_name(name: str) -> str:
    first = clean_text(name).split(" ", 1)[0] if clean_text(name) else ""
    letter = next((ch.upper() for ch in first if ch.isalpha()), "A")

    if "A" <= letter <= "D":
        return "A-D"
    if "E" <= letter <= "J":
        return "E-J"
    if "K" <= letter <= "O":
        return "K-O"
    return "P-Z"


def parse_level_position(raw_role: str) -> tuple[str, str]:
    role = re.sub(r"\s+", " ", raw_role.strip())
    lower = role.lower()

    if lower.endswith("assistant coach"):
        return role[: -len("Assistant Coach")].strip(), "Assistant Coach"
    if lower.endswith("head coach"):
        return role[: -len("Head Coach")].strip(), "Head Coach"

    return role, "Unknown"


def get_int_env_var(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"Invalid {name} value '{raw}', defaulting to {default}.")
        return default
    return max(0, value)


def env_ms(name: str) -> int:
    return get_int_env_var(name, DEFAULT_INT_ENV[name])


def _playwright_in_thread(fn):
    """Run fn() in a fresh OS thread.

    sync_playwright() cannot be used inside a running asyncio event loop (which
    GitHub Actions / Python 3.11 may have active).  A plain OS thread starts
    with no running loop, so sync_playwright works correctly there.
    """
    with _ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(fn).result()


def get_rendered_html(url: str) -> str:
    goto_timeout = env_ms("SCRAPE_GOTO_TIMEOUT_MS")
    network_idle_timeout = env_ms("SCRAPE_NETWORK_IDLE_TIMEOUT_MS")
    selector_timeout = env_ms("SCRAPE_SELECTOR_TIMEOUT_MS")

    try:
        def _pw_run() -> str:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=goto_timeout)

                # The source site appears to load rows asynchronously after initial paint.
                page.wait_for_timeout(6000)
                page.wait_for_load_state("networkidle", timeout=network_idle_timeout)

                try:
                    page.wait_for_selector("tr td", timeout=selector_timeout)
                except PlaywrightTimeoutError:
                    # Keep parsing whatever content is available.
                    pass

                html = page.content()
                browser.close()
                return html

        return _playwright_in_thread(_pw_run)
    except Exception:
        # Fallback for environments where Playwright/browser installation is unavailable.
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        return response.text


def fetch_cloud_data_items(url: str) -> List[dict]:
    def _pw_run() -> List[dict]:
        captured: Dict[str, dict] = {}

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            def on_response(resp) -> None:
                if "/_api/cloud-data/v2/items/query" not in resp.url or resp.status != 200:
                    return

                try:
                    payload = resp.json()
                except Exception:
                    return

                items = payload.get("items") or payload.get("dataItems") or []
                for item in items:
                    item_id = item.get("id") or item.get("_id")
                    if item_id:
                        captured[item_id] = item

            page.on("response", on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(3000)
            browser.close()

        return list(captured.values())

    return _playwright_in_thread(_pw_run)


def rows_from_cloud_items(items: Sequence[dict], source_url: str) -> List[CoachRow]:
    rows: List[CoachRow] = []
    for item in items:
        data = item.get("data", {})
        name = str(data.get("title", "")).strip()
        reg_id = str(data.get("nccp", "")).strip()
        raw_role = str(data.get("position", "")).strip()
        association = str(data.get("team", "")).strip()

        if not name or not association or not raw_role:
            continue

        level, position = parse_level_position(raw_role)
        rows.append(
            CoachRow(
                name=name,
                registration_id=reg_id,
                level=level,
                position=position,
                association=association,
                source_url=source_url,
            )
        )

    return rows


def fetch_rows(url: str) -> List[CoachRow]:
    cloud_items = fetch_cloud_data_items(url)
    if cloud_items:
        return rows_from_cloud_items(cloud_items, url)

    html = get_rendered_html(url)

    soup = BeautifulSoup(html, "html.parser")
    rows: List[CoachRow] = []

    for tr in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 4:
            continue

        name = cells[0]
        reg_id = cells[1]
        raw_role = cells[2]
        association = cells[3]

        if not name or not association:
            continue

        # Skip obvious non-data rows.
        if normalize(name) in {"loading...", "name"}:
            continue

        level, position = parse_level_position(raw_role)
        rows.append(
            CoachRow(
                name=name,
                registration_id=reg_id,
                level=level,
                position=position,
                association=association,
                source_url=url,
            )
        )

    return rows


def filter_sarnia(rows: Sequence[CoachRow]) -> List[CoachRow]:
    return [row for row in rows if "sarnia" in normalize(row.association)]


def load_previous_status() -> dict:
    if STATUS_PATH.exists():
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    return {}


def as_rows(records: Sequence[dict]) -> List[CoachRow]:
    output: List[CoachRow] = []
    for r in records:
        output.append(
            CoachRow(
                name=r.get("name", ""),
                registration_id=r.get("registration_id", ""),
                level=r.get("level", ""),
                position=r.get("position", ""),
                association=r.get("association", ""),
                source_url=r.get("source_url", ""),
            )
        )
    return output


def diff_rows(previous: Sequence[CoachRow], current: Sequence[CoachRow]) -> tuple[List[CoachRow], List[CoachRow]]:
    previous_map = {row.key(): row for row in previous}
    current_map = {row.key(): row for row in current}

    added = [current_map[key] for key in current_map.keys() - previous_map.keys()]
    removed = [previous_map[key] for key in previous_map.keys() - current_map.keys()]
    return sort_rows(added), sort_rows(removed)


def compute_transitions(prev_in_progress: Sequence[CoachRow], new_certified: Sequence[CoachRow]) -> List[dict]:
    prev_map = {row.key(): row for row in prev_in_progress}
    moved = [row for row in new_certified if row.key() in prev_map]

    return [
        {
            "name": row.name,
            "registration_id": row.registration_id,
            "level": row.level,
            "position": row.position,
            "association": row.association,
            "moved_on": str(date.today()),
        }
        for row in moved
    ]


def sort_rows(rows: Sequence[CoachRow]) -> List[CoachRow]:
    return sorted(rows, key=lambda r: (normalize(r.name), normalize(r.level), normalize(r.position)))


def query_timestamps() -> tuple[str, str, str]:
    now_utc = datetime.now(timezone.utc)
    try:
        now_local = now_utc.astimezone(ZoneInfo("America/Toronto"))
    except Exception:
        now_local = now_utc.astimezone()
    return (
        now_local.strftime("%Y-%m-%d"),
        now_local.strftime("%Y-%m-%d %H:%M:%S %Z"),
        now_utc.isoformat().replace("+00:00", "Z"),
    )


def fetch_certification_status_dates() -> dict:
    html = get_rendered_html(COACH_STATUS_URL)
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    compact = re.sub(r"\s+", " ", text)

    date_updated_match = re.search(
        r"Date Updated\s*:\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})",
        compact,
        flags=re.IGNORECASE,
    )
    next_update_match = re.search(
        r"Next Scheduled Update\s*:\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})",
        compact,
        flags=re.IGNORECASE,
    )
    note_match = re.search(
        r"Courses after this date will be on the next update\.",
        compact,
        flags=re.IGNORECASE,
    )

    return {
        "source": COACH_STATUS_URL,
        "dateUpdated": date_updated_match.group(1) if date_updated_match else "Unknown",
        "nextScheduledUpdate": next_update_match.group(1) if next_update_match else "Unknown",
        "note": note_match.group(0) if note_match else "",
    }


def wait_for_coach_status_page(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=env_ms("COACH_STATUS_PAGE_GOTO_TIMEOUT_MS"))
    page.wait_for_timeout(env_ms("MISSING_COURSE_BUCKET_SETTLE_MS"))
    try:
        page.wait_for_load_state("networkidle", timeout=env_ms("COACH_STATUS_PAGE_NETWORK_IDLE_TIMEOUT_MS"))
    except PlaywrightTimeoutError:
        pass
    page.locator('input[role="combobox"]').first.wait_for(timeout=env_ms("COACH_STATUS_PAGE_COMBO_TIMEOUT_MS"))


def is_missing_course_value(value: str) -> bool:
    return normalize(value) in MISSING_COURSE_NEGATIVE_VALUES


def is_truthy_env_var(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def extract_missing_courses_from_page(page, row: CoachRow) -> tuple[List[str], bool, str]:
    body_text = clean_text(page.locator("body").inner_text())
    body_norm = normalize(body_text)
    name_norm = normalize(row.name)
    role_norm = normalize(coach_role_label(row))
    assoc_norm = normalize(row.association) if row.association else ""
    reg_id_norm = normalize(row.registration_id) if row.registration_id else ""

    if name_norm and name_norm not in body_norm:
        return [], False, "name_mismatch"
    if role_norm and role_norm not in body_norm:
        return [], False, "role_mismatch"

    notes = []
    if reg_id_norm and reg_id_norm not in body_norm:
        notes.append("registration_id_not_visible")
    if assoc_norm and assoc_norm not in body_norm:
        notes.append("association_not_visible")

    missing_courses: List[str] = []
    tables = page.locator("table")
    observed_values = set()

    for index in range(tables.count()):
        rows = tables.nth(index).locator("tr")
        if rows.count() < 2:
            continue

        headers = [clean_text(value) for value in rows.nth(0).locator("th").all_inner_texts()]
        values = [clean_text(value) for value in rows.nth(1).locator("td").all_inner_texts()]

        for header, value in zip(headers, values):
            normalized_value = normalize(value)
            if normalized_value:
                observed_values.add(normalized_value)
            if header and is_missing_course_value(value):
                missing_courses.append(header)

    unique_missing_courses = list(dict.fromkeys(missing_courses))
    if not observed_values:
        return unique_missing_courses, False, "no_course_status_tokens"
    if not unique_missing_courses and observed_values and observed_values.isdisjoint(MISSING_COURSE_NEGATIVE_VALUES | {"yes"}):
        notes.append(f"unknown_status_tokens:{','.join(sorted(observed_values)[:5])}")

    if notes:
        return unique_missing_courses, True, ";".join(notes)

    return unique_missing_courses, True, "ok"


def _fetch_bucket_api_items(url: str, bucket: str) -> List[dict]:
    """Fetch NonCertifiedCoaches items for a bucket via page.evaluate fetch().

    Uses the browser's own auth context so no manual token handling is needed.
    Queries the Wix cloud-data API directly with a high limit to return all
    coaches in the bucket in one call, with pagination if needed.
    """
    query: dict = {
        "dataCollectionId": _NONCERTIFIED_COLLECTION,
        "query": {
            "filter": {"sort": {"$contains": bucket}},
            "sort": [{"fieldName": "title", "order": "ASC"}],
            "paging": {"offset": 0, "limit": 1000},
            "fields": [],
        },
        "referencedItemOptions": [],
        "returnTotalCount": True,
        "environment": "LIVE",
        "appId": _NONCERTIFIED_APP_ID,
    }

    def _pw_run() -> List[dict]:
        all_items: List[dict] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 2400})
            page.goto(url, wait_until="domcontentloaded", timeout=env_ms("COACH_STATUS_PAGE_GOTO_TIMEOUT_MS"))
            page.wait_for_timeout(env_ms("MISSING_COURSE_BUCKET_SETTLE_MS"))

            while True:
                result = page.evaluate(
                    """
                    async (queryJson) => {
                        try {
                            const b64 = btoa(unescape(encodeURIComponent(queryJson)));
                            const resp = await fetch('/_api/cloud-data/v2/items/query?.r=' + b64);
                            if (!resp.ok) return {error: resp.status, items: []};
                            return await resp.json();
                        } catch(e) {
                            return {error: String(e), items: []};
                        }
                    }
                    """,
                    json.dumps(query),
                )
                if "error" in result:
                    print(f"  _fetch_bucket_api_items error: {result['error']}")
                    break
                batch = result.get("items") or result.get("dataItems") or []
                all_items.extend(batch)
                total = (
                    result.get("totalCount")
                    or (result.get("pagingMetadata") or {}).get("total")
                    or 0
                )
                print(f"  Bucket {bucket}: fetched {len(all_items)}/{total} items (offset={query['query']['paging']['offset']})")
                if not batch or len(all_items) >= total:
                    break
                query["query"]["paging"]["offset"] += len(batch)

            browser.close()

        return all_items

    return _playwright_in_thread(_pw_run)


def _match_api_items_to_rows(items: List[dict], rows: Sequence[CoachRow]) -> Dict[str, dict]:
    """Match NonCertifiedCoaches items to in-progress rows; extract missing courses from field values.

    Course fields are any data field not in _NON_COURSE_FIELDS. A field with a
    normalised value in MISSING_COURSE_NEGATIVE_VALUES (e.g. 'no') is a missing
    course. Fields with 'Yes' are complete; 'NR' means not required.
    Always logs field names so CI output shows the full API structure.
    """
    if not items:
        return {}

    # Log sample field names for CI diagnostics.
    sample_data = items[0].get("data", {})
    print(f"API item data fields (sample): {sorted(sample_data.keys())}")

    # Build name index: strip level suffix from title e.g. 'Aaron Adams  (10U A)' -> 'aaron adams'.
    item_by_name: Dict[str, dict] = {}
    for item in items:
        data = item.get("data", {})
        raw_title = str(data.get("title", "")).strip()
        name_only = re.sub(r"\s*\(.*?\)\s*$", "", raw_title).strip()
        if name_only:
            item_by_name[normalize(name_only)] = data

    # Field families to log in full for CI diagnostics (all numbered variants).
    _DIAG_FAMILIES = ("practiceEvaluation", "videoPackage", "classification")

    captured: Dict[str, dict] = {}
    for row in rows:
        data = item_by_name.get(normalize(row.name))
        if data is None:
            continue

        # Diagnostic: log ambiguous field families AND every field with a "No" value
        # (before _NON_COURSE_FIELDS filtering) to surface unknown fields like "9U PCCP".
        diag_fields = {
            k: v for k, v in data.items()
            if any(k.lower().startswith(fam.lower()) for fam in _DIAG_FAMILIES)
            or (isinstance(v, str) and normalize(v) in MISSING_COURSE_NEGATIVE_VALUES)
        }
        if diag_fields:
            print(f"  DIAG {row.name}: {diag_fields}")

        missing = [
            _COURSE_FIELD_DISPLAY_NAMES.get(key, key)
            for key, val in data.items()
            if key not in _NON_COURSE_FIELDS
            and not key.startswith("_")
            and isinstance(val, str)
            and normalize(val) in MISSING_COURSE_NEGATIVE_VALUES
        ]
        captured[row.key()] = {
            "missing_courses": missing,
            "missing_courses_available": True,
            "missing_courses_reason": "api_interception",
        }

    return captured


def _log_page_state(page, label: str) -> None:
    """Print a compact DOM snapshot to stdout for CI log triage."""
    try:
        info = page.evaluate("""
            () => {
                const c = document.querySelector('input[role="combobox"]') ||
                          document.querySelector('[role="combobox"]');
                return {
                    combobox:      document.querySelectorAll('[role="combobox"]').length,
                    option:        document.querySelectorAll('[role="option"]').length,
                    listbox:       document.querySelectorAll('[role="listbox"]').length,
                    li:            document.querySelectorAll('li').length,
                    aria_expanded: document.querySelectorAll('[aria-expanded]').length,
                    comboValue:    c ? c.value : 'NOT_FOUND',
                    comboTag:      c ? c.tagName : '',
                    comboRO:       c ? c.readOnly : null,
                    parentTag:     c && c.parentElement ? c.parentElement.tagName : '',
                    parentRole:    c && c.parentElement ? (c.parentElement.getAttribute('role') || '') : '',
                    parentClass:   c && c.parentElement ? c.parentElement.className.toString().slice(0, 120) : '',
                    sampleLi:      Array.from(document.querySelectorAll('li')).slice(0, 4).map(l => l.textContent.trim().slice(0, 50)),
                };
            }
        """)
        print(f"[PAGE_STATE for '{label}'] {info}")
    except Exception as exc:
        print(f"[PAGE_STATE] eval failed: {exc}")


def _ui_select_coach(page, row: CoachRow, label: str, combo_timeout: int, option_timeout: int) -> str:
    """Attempt to select a coach via UI dropdown. Raises PlaywrightTimeoutError on failure."""
    name_norm = normalize(row.name)

    def _js_click_match() -> str | None:
        return page.evaluate("""
            (nameNorm) => {
                function norm(s) { return s.toLowerCase().replace(/\\s+/g, ' ').trim(); }
                function visible(el) {
                    const r = el.getBoundingClientRect();
                    if (!r.width || !r.height) return false;
                    const s = window.getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden';
                }
                for (const el of document.querySelectorAll('[role="option"],[role="listitem"],[data-hook],li,[tabindex]')) {
                    if (visible(el) && norm(el.textContent).includes(nameNorm)) {
                        el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
                        el.dispatchEvent(new MouseEvent('click', {bubbles:true}));
                        return el.textContent.trim();
                    }
                }
                return null;
            }
        """, name_norm)

    def _open_via_js() -> None:
        page.evaluate("""
            () => {
                const input = document.querySelector('input[role="combobox"]');
                const container = document.querySelector('[role="combobox"]') || input;
                if (!container) return;
                for (const el of [container, input].filter(Boolean)) {
                    el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
                    el.dispatchEvent(new MouseEvent('click',     {bubbles:true, cancelable:true}));
                }
                if (input) input.focus();
            }
        """)

    # Strategy 1: click the [role="combobox"] CONTAINER first, then type.
    try:
        page.locator('[role="combobox"]').first.click(timeout=combo_timeout)
        page.wait_for_timeout(400)
        inp = page.locator('input[role="combobox"]').first
        inp.triple_click(timeout=combo_timeout)
        inp.press_sequentially(label, delay=50)
        page.wait_for_timeout(700)
        result = _js_click_match()
        if result:
            return result
    except Exception:
        pass

    # Strategy 2: JS open + type first-name prefix.
    try:
        _open_via_js()
        page.wait_for_timeout(400)
        inp = page.locator('input[role="combobox"]').first
        inp.triple_click(timeout=combo_timeout)
        inp.press_sequentially(row.name.split()[0], delay=60)
        page.wait_for_timeout(800)
        result = _js_click_match()
        if result:
            return result
    except Exception:
        pass

    # Strategy 3: Escape reset, then ArrowDown to force-open.
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        _open_via_js()
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(600)
        result = _js_click_match()
        if result:
            return result
    except Exception:
        pass

    _log_page_state(page, label)
    raise PlaywrightTimeoutError(f"UI selection failed for '{label}'")


def fetch_missing_courses_for_in_progress(rows: Sequence[CoachRow]) -> Dict[str, dict]:
    global LAST_MISSING_COURSE_DIAGNOSTICS

    if not rows:
        LAST_MISSING_COURSE_DIAGNOSTICS = {}
        return {}

    grouped_rows: Dict[str, List[CoachRow]] = {bucket: [] for bucket in COACH_STATUS_BY_BUCKET_URLS}
    for row in rows:
        grouped_rows[bucket_for_name(row.name)].append(row)

    captured: Dict[str, dict] = {}
    total_lookup_failures = 0
    total_available_true = 0
    total_available_false = 0
    reason_counts: Dict[str, int] = {}
    sample_unavailable_labels: List[str] = []

    combo_timeout = env_ms("MISSING_COURSE_COMBO_TIMEOUT_MS")
    option_timeout = env_ms("MISSING_COURSE_OPTION_TIMEOUT_MS")
    lookup_timeout = env_ms("MISSING_COURSE_LOOKUP_TIMEOUT_MS")
    selection_settle_ms = env_ms("MISSING_COURSE_SELECTION_SETTLE_MS")
    lookup_retries = env_ms("MISSING_COURSE_LOOKUP_RETRIES")
    retry_backoff_ms = env_ms("MISSING_COURSE_RETRY_BACKOFF_MS")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 2400})

            for bucket in ("A-D", "E-J", "K-O", "P-Z"):
                bucket_rows = sort_rows(grouped_rows.get(bucket, []))
                if not bucket_rows:
                    continue

                bucket_available_true = 0
                bucket_available_false = 0
                bucket_lookup_failures = 0

                # ── Primary: API interception (same pattern as certified/in-progress) ──
                bucket_url = COACH_STATUS_BY_BUCKET_URLS[bucket]
                print(f"Bucket {bucket}: attempting API interception from {bucket_url}")
                api_items = _fetch_bucket_api_items(bucket_url, bucket)
                print(f"Bucket {bucket}: captured {len(api_items)} API items")
                api_captured = _match_api_items_to_rows(api_items, bucket_rows)

                if api_captured:
                    print(f"Bucket {bucket}: API interception succeeded for {len(api_captured)} rows")
                    for row in bucket_rows:
                        details = api_captured.get(row.key(), {})
                        available = bool(details)
                        if available:
                            bucket_available_true += 1
                            total_available_true += 1
                        else:
                            bucket_available_false += 1
                            total_available_false += 1
                        reason = details.get("missing_courses_reason", "api_no_match")
                        reason_counts[reason] = reason_counts.get(reason, 0) + 1
                        captured[row.key()] = {
                            "missing_courses": details.get("missing_courses", []),
                            "missing_courses_available": available,
                            "missing_courses_reason": reason,
                        }
                    print(
                        f"Bucket {bucket}: total={len(bucket_rows)}, "
                        f"available={bucket_available_true}, unavailable={bucket_available_false}"
                    )
                    continue  # skip UI fallback for this bucket

                # ── Fallback: UI dropdown interaction ──
                print(f"Bucket {bucket}: API interception returned no course data; trying UI fallback")
                wait_for_coach_status_page(page, bucket_url)

                for row in bucket_rows:
                    label = coach_dropdown_label(row)
                    last_error = ""
                    succeeded = False

                    for attempt in range(lookup_retries + 1):
                        try:
                            selected_label = _ui_select_coach(
                                page, row, label, combo_timeout, option_timeout
                            )

                            page.wait_for_function(
                                "({ name, roleLabel }) => document.body.innerText.toLowerCase().includes(name.toLowerCase()) && document.body.innerText.includes(roleLabel)",
                                arg={"name": row.name, "roleLabel": coach_role_label(row)},
                                timeout=lookup_timeout,
                            )
                            page.wait_for_timeout(selection_settle_ms)

                            missing_courses, available, reason = extract_missing_courses_from_page(page, row)
                            reason_counts[reason] = reason_counts.get(reason, 0) + 1

                            if available:
                                bucket_available_true += 1
                                total_available_true += 1
                            else:
                                bucket_available_false += 1
                                total_available_false += 1
                                if len(sample_unavailable_labels) < 10:
                                    sample_unavailable_labels.append(label)

                            captured[row.key()] = {
                                "missing_courses": missing_courses,
                                "missing_courses_available": available,
                                "missing_courses_reason": reason,
                            }
                            if selected_label != label:
                                print(f"Missing-course lookup used fallback option match: requested='{label}' matched='{selected_label}'")
                            succeeded = True
                            break
                        except PlaywrightTimeoutError as exc:
                            last_error = f"timeout:{exc}"
                        except Exception as exc:
                            last_error = f"exception:{exc}"

                        if attempt < lookup_retries:
                            page.wait_for_timeout(retry_backoff_ms * (attempt + 1))

                    if succeeded:
                        continue

                    failure_reason = "lookup_timeout_or_error"
                    reason_counts[failure_reason] = reason_counts.get(failure_reason, 0) + 1
                    bucket_lookup_failures += 1
                    total_lookup_failures += 1
                    bucket_available_false += 1
                    total_available_false += 1
                    if len(sample_unavailable_labels) < 10:
                        sample_unavailable_labels.append(label)
                    captured[row.key()] = {
                        "missing_courses": [],
                        "missing_courses_available": False,
                        "missing_courses_reason": failure_reason,
                    }
                    print(f"Missing-course lookup failed for {label}: {last_error}")

                print(
                    "Missing-course extraction bucket "
                    f"{bucket}: total={len(bucket_rows)}, available={bucket_available_true}, "
                    f"unavailable={bucket_available_false}, failures={bucket_lookup_failures}"
                )

            browser.close()
    except Exception as exc:
        print(f"Coach status scraping unavailable: {exc}")

    print(
        "Missing-course extraction summary: "
        f"total={len(rows)}, captured={len(captured)}, available={total_available_true}, "
        f"unavailable={total_available_false}, failures={total_lookup_failures}, reasons={reason_counts}"
    )

    LAST_MISSING_COURSE_DIAGNOSTICS = {
        "total": len(rows),
        "captured": len(captured),
        "available": total_available_true,
        "unavailable": total_available_false,
        "failures": total_lookup_failures,
        "reasons": dict(sorted(reason_counts.items(), key=lambda item: item[0])),
        "sample_unavailable_labels": sample_unavailable_labels,
        "lookup_retries": lookup_retries,
        "lookup_timeout_ms": lookup_timeout,
    }

    return captured


def enforce_missing_course_coverage_or_fail(in_progress_rows: Sequence[CoachRow]) -> None:
    if not in_progress_rows:
        return

    available_count = sum(1 for row in in_progress_rows if row.missing_courses_available)
    coverage = available_count / len(in_progress_rows)

    threshold = MISSING_COURSE_MIN_COVERAGE
    threshold_raw = os.environ.get("MISSING_COURSE_MIN_COVERAGE", "").strip()
    if threshold_raw:
        try:
            threshold = max(0.0, min(1.0, float(threshold_raw)))
        except ValueError:
            print(
                "Invalid MISSING_COURSE_MIN_COVERAGE value "
                f"'{threshold_raw}', defaulting to {MISSING_COURSE_MIN_COVERAGE:.0%}."
            )

    print(
        "Missing-course coverage check: "
        f"available={available_count}/{len(in_progress_rows)} ({coverage:.1%}), threshold={threshold:.1%}"
    )

    if coverage >= threshold:
        return

    if is_truthy_env_var("ALLOW_LOW_MISSING_COURSE_COVERAGE"):
        print("Low missing-course coverage override enabled; continuing run.")
        return

    unavailable_count = len(in_progress_rows) - available_count
    diagnostics = LAST_MISSING_COURSE_DIAGNOSTICS if LAST_MISSING_COURSE_DIAGNOSTICS else {}
    reasons = diagnostics.get("reasons", {})
    sample_labels = diagnostics.get("sample_unavailable_labels", [])
    retry_budget = diagnostics.get("lookup_retries", env_ms("MISSING_COURSE_LOOKUP_RETRIES"))
    lookup_timeout = diagnostics.get("lookup_timeout_ms", env_ms("MISSING_COURSE_LOOKUP_TIMEOUT_MS"))

    raise RuntimeError(
        "Missing-course extraction coverage fell below threshold "
        f"({coverage:.1%} < {threshold:.1%}). Failing run to avoid publishing degraded data. "
        f"Diagnostics: available={available_count}, unavailable={unavailable_count}, "
        f"reasons={reasons}, sample_unavailable={sample_labels}, "
        f"lookup_retries={retry_budget}, lookup_timeout_ms={lookup_timeout}. "
        "Set ALLOW_LOW_MISSING_COURSE_COVERAGE=true to bypass intentionally."
    )


def attach_missing_courses(rows: Sequence[CoachRow], missing_courses_map: Dict[str, dict]) -> List[CoachRow]:
    updated_rows: List[CoachRow] = []

    for row in rows:
        details = missing_courses_map.get(row.key(), {})
        updated_rows.append(
            CoachRow(
                name=row.name,
                registration_id=row.registration_id,
                level=row.level,
                position=row.position,
                association=row.association,
                source_url=row.source_url,
                missing_courses=list(details.get("missing_courses", [])),
                missing_courses_available=bool(details.get("missing_courses_available", False)),
                missing_courses_reason=str(details.get("missing_courses_reason", "")),
            )
        )

    return updated_rows


def render_rows_table(rows: Sequence[CoachRow]) -> str:
    if not rows:
        return "<p class=\"empty\">No rows found.</p>"

    table_rows = []
    for row in rows:
        table_rows.append(
            "<tr>"
            f"<td>{escape(row.name)}</td>"
            f"<td>{escape(row.level)}</td>"
            f"<td>{escape(row.position)}</td>"
            f"<td>{escape(row.association)}</td>"
            f"<td><a href=\"{escape(row.source_url)}\" target=\"_blank\" rel=\"noreferrer\">View</a></td>"
            "</tr>"
        )

    return (
        "<div class=\"panel\">"
        "<table>"
        "<thead><tr><th>Name</th><th>Level</th><th>Position</th><th>Association</th><th>Source</th></tr></thead>"
        f"<tbody>{''.join(table_rows)}</tbody>"
        "</table>"
        "</div>"
    )


def format_row_brief(row: CoachRow) -> str:
    return f"{row.name} | {row.level} | {row.position} | {row.association}"


def append_row_section(lines: List[str], heading: str, rows: Sequence[CoachRow], limit: int = 5) -> None:
    if not rows:
        return

    lines.append(f"{heading} ({len(rows)}):")
    for row in list(rows)[:limit]:
        lines.append(f"- {format_row_brief(row)}")
    if len(rows) > limit:
        lines.append(f"- ...and {len(rows) - limit} more")


def build_slack_message(
    queried_at_local: str,
    previous_status: dict,
    certification_status: dict,
    added_certified: Sequence[CoachRow],
    removed_certified: Sequence[CoachRow],
    added_in_progress: Sequence[CoachRow],
    removed_in_progress: Sequence[CoachRow],
    transitions: Sequence[dict],
    certified_count: int,
    in_progress_count: int,
) -> str:
    lines = [
        "Sarnia Coaches Checker update",
        f"Last query: {queried_at_local}",
        f"Current counts: Certified {certified_count} | In Progress {in_progress_count}",
        "",
    ]

    append_row_section(lines, "Certified added", added_certified)
    append_row_section(lines, "Certified removed", removed_certified)
    append_row_section(lines, "In Progress added", added_in_progress)
    append_row_section(lines, "In Progress removed", removed_in_progress)

    if transitions:
        lines.append(f"Transitions to Certified ({len(transitions)}):")
        for item in list(transitions)[:5]:
            lines.append(
                f"- {item.get('name', 'Unknown')} | {item.get('level', 'N/A')} | {item.get('position', 'N/A')} | {item.get('association', 'N/A')}"
            )
        if len(transitions) > 5:
            lines.append(f"- ...and {len(transitions) - 5} more")

    prev_cert_status = previous_status.get("certificationStatus", {})
    prev_date_updated = prev_cert_status.get("dateUpdated")
    prev_next_update = prev_cert_status.get("nextScheduledUpdate")

    if prev_date_updated and prev_date_updated != certification_status.get("dateUpdated"):
        lines.append(
            f"OBA Date Updated changed: {prev_date_updated} -> {certification_status.get('dateUpdated', 'Unknown')}"
        )
    if prev_next_update and prev_next_update != certification_status.get("nextScheduledUpdate"):
        lines.append(
            f"OBA Next Scheduled Update changed: {prev_next_update} -> {certification_status.get('nextScheduledUpdate', 'Unknown')}"
        )

    return "\n".join(line for line in lines if line is not None)


def send_slack_notification(message: str) -> None:
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("Slack webhook not configured; skipping Slack notification.")
        return

    response = requests.post(webhook_url, json={"text": message}, timeout=20)
    response.raise_for_status()
    print("Slack notification sent.")


def force_slack_test_requested() -> bool:
    return os.environ.get("FORCE_SLACK_TEST", "false").strip().lower() in {"1", "true", "yes", "on"}


def build_slack_test_message(
    queried_at_local: str,
    certified_count: int,
    in_progress_count: int,
    certification_status: dict,
) -> str:
    return "\n".join(
        [
            "Sarnia Coaches Checker test notification",
            f"Last query: {queried_at_local}",
            f"Current counts: Certified {certified_count} | In Progress {in_progress_count}",
            f"OBA Date Updated: {certification_status.get('dateUpdated', 'Unknown')}",
            f"OBA Next Scheduled Update: {certification_status.get('nextScheduledUpdate', 'Unknown')}",
            "This is a temporary manual test message from GitHub Actions workflow_dispatch.",
        ]
    )


def maybe_notify_slack(
    previous: dict,
    queried_at_local: str,
    certification_status: dict,
    certified: Sequence[CoachRow],
    in_progress: Sequence[CoachRow],
    transitions: Sequence[dict],
) -> None:
    if force_slack_test_requested():
        try:
            send_slack_notification(
                build_slack_test_message(
                    queried_at_local=queried_at_local,
                    certified_count=len(certified),
                    in_progress_count=len(in_progress),
                    certification_status=certification_status,
                )
            )
        except Exception as exc:
            print(f"Slack test notification failed: {exc}")
        return

    if not previous:
        print("No previous baseline found; skipping Slack notification for initial dataset.")
        return

    previous_certified = as_rows(previous.get("certified", []))
    previous_in_progress = as_rows(previous.get("inProgress", []))

    added_certified, removed_certified = diff_rows(previous_certified, certified)
    added_in_progress, removed_in_progress = diff_rows(previous_in_progress, in_progress)

    prev_cert_status = previous.get("certificationStatus", {})
    date_changed = prev_cert_status.get("dateUpdated") not in {None, certification_status.get("dateUpdated")}
    next_update_changed = prev_cert_status.get("nextScheduledUpdate") not in {None, certification_status.get("nextScheduledUpdate")}

    has_changes = any(
        [
            added_certified,
            removed_certified,
            added_in_progress,
            removed_in_progress,
            transitions,
            date_changed,
            next_update_changed,
        ]
    )

    if not has_changes:
        print("No meaningful changes detected; skipping Slack notification.")
        return

    message = build_slack_message(
        queried_at_local=queried_at_local,
        previous_status=previous,
        certification_status=certification_status,
        added_certified=added_certified,
        removed_certified=removed_certified,
        added_in_progress=added_in_progress,
        removed_in_progress=removed_in_progress,
        transitions=transitions,
        certified_count=len(certified),
        in_progress_count=len(in_progress),
    )

    try:
        send_slack_notification(message)
    except Exception as exc:
        print(f"Slack notification failed: {exc}")


def write_summary_page(
    certified: Sequence[CoachRow],
    in_progress: Sequence[CoachRow],
    transitions: Sequence[dict],
    queried_at_local: str,
    certification_status: dict,
) -> None:
    transition_items = "".join(
        "<li>"
        f"{escape(item.get('name', 'Unknown'))} moved to Certified "
        f"({escape(item.get('level', 'N/A'))} - {escape(item.get('position', 'N/A'))})"
        "</li>"
        for item in transitions
    )
    transition_html = (
        f"<ul>{transition_items}</ul>" if transition_items else "<p class=\"empty\">No new in-progress to certified transitions detected.</p>"
    )

    html = f"""<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>Current Summary | Sarnia Coaches Checker</title>
    <style>
        :root {{
            --ink: #10212e;
            --paper: #f6f5ef;
            --card: #ffffff;
            --line: #d7dee4;
            --accent: #0f4e66;
        }}
        body {{
            margin: 0;
            font-family: "Segoe UI", Tahoma, sans-serif;
            background: var(--paper);
            color: var(--ink);
        }}
        .wrap {{
            width: min(1200px, 95vw);
            margin: 24px auto 40px;
        }}
        .top {{
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            gap: 12px;
            flex-wrap: wrap;
        }}
        h1, h2 {{ margin: 0; }}
        .meta {{ margin: 8px 0 18px; opacity: 0.8; }}
        .card {{
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 16px;
        }}
        .panel {{
            border: 1px solid var(--line);
            border-radius: 10px;
            overflow: auto;
            margin-top: 12px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            min-width: 760px;
        }}
        th, td {{
            text-align: left;
            padding: 10px 12px;
            border-bottom: 1px solid var(--line);
            vertical-align: top;
        }}
        th {{
            background: #edf3f7;
            font-size: 0.9rem;
        }}
        .empty {{
            margin: 8px 0 0;
            opacity: 0.75;
            font-style: italic;
        }}
        a {{ color: var(--accent); }}
    </style>
</head>
<body>
    <main class=\"wrap\">
        <div class=\"top\">
            <h1>Current Summary</h1>
            <a href=\"./index.html\">Back to dashboard</a>
        </div>
        <p class=\"meta\">Last query: {queried_at_local} | Certified: {len(certified)} | In Progress: {len(in_progress)}</p>


        <section class=\"card\">
            <h2>Detected Transitions</h2>
            {transition_html}
        </section>

        <section class=\"card\">
            <h2>Certification Status Dates</h2>
            <p><strong>Date Updated:</strong> {escape(certification_status.get("dateUpdated", "Unknown"))}</p>
            <p><strong>Next Scheduled Update:</strong> {escape(certification_status.get("nextScheduledUpdate", "Unknown"))}</p>
            <p>{escape(certification_status.get("note", ""))}</p>
            <p><a href=\"{escape(certification_status.get("source", COACH_STATUS_URL))}\" target=\"_blank\" rel=\"noreferrer\">Open Coach Certification Status Source</a></p>
        </section>

        <section class=\"card\">
            <h2>Certified Coaches</h2>
            {render_rows_table(certified)}
        </section>

        <section class=\"card\">
            <h2>In Progress Coaches</h2>
            {render_rows_table(in_progress)}
        </section>
    </main>
</body>
</html>
"""

    SUMMARY_PATH.write_text(html, encoding="utf-8")


def main() -> None:
    certified_all = fetch_rows(CERTIFIED_URL)
    in_progress_all = fetch_rows(IN_PROGRESS_URL)
    certification_status = fetch_certification_status_dates()

    as_of_date, queried_at_local, queried_at_utc = query_timestamps()

    certified = sort_rows(filter_sarnia(certified_all))
    in_progress = sort_rows(filter_sarnia(in_progress_all))
    in_progress_missing_courses = _playwright_in_thread(
        lambda: fetch_missing_courses_for_in_progress(in_progress)
    )
    in_progress = attach_missing_courses(in_progress, in_progress_missing_courses)
    enforce_missing_course_coverage_or_fail(in_progress)

    previous = load_previous_status()
    prev_in_progress = as_rows(previous.get("inProgress", []))
    transitions = compute_transitions(prev_in_progress, certified)

    payload = {
        "asOf": as_of_date,
        "queriedAtLocal": queried_at_local,
        "queriedAtUtc": queried_at_utc,
        "sources": {
            "certified": CERTIFIED_URL,
            "inProgress": IN_PROGRESS_URL,
            "coachCertificationStatus": COACH_STATUS_URL,
        },
        "certificationStatus": certification_status,
        "certified": [asdict(r) for r in certified],
        "inProgress": [asdict(r) for r in in_progress],
        "transitions": transitions,
        "notes": [
            "Rows are filtered to associations containing 'Sarnia'.",
            "A transition is detected when a previously in-progress coach row appears in certified.",
            "In Progress rows may include missing_courses when coach-status extraction succeeds.",
        ],
    }

    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_summary_page(certified, in_progress, transitions, queried_at_local, certification_status)
    maybe_notify_slack(previous, queried_at_local, certification_status, certified, in_progress, transitions)
    print(
        f"Updated {STATUS_PATH} with {len(certified)} certified and {len(in_progress)} in-progress rows. "
        f"Raw rows: certified={len(certified_all)}, in-progress={len(in_progress_all)}"
    )


if __name__ == "__main__":
    main()
