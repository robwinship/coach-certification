import json
import re
from dataclasses import dataclass, asdict
from datetime import date
from html import escape
from pathlib import Path
from typing import Dict, List, Sequence

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

CERTIFIED_URL = "https://www.registeroba.ca/certified-coaches"
IN_PROGRESS_URL = "https://www.registeroba.ca/certification-inprogress-by-local"
STATUS_PATH = Path("docs/status.json")
SUMMARY_PATH = Path("docs/current-summary.html")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}


@dataclass
class CoachRow:
    name: str
    registration_id: str
    level: str
    position: str
    association: str
    source_url: str

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


def parse_level_position(raw_role: str) -> tuple[str, str]:
    role = re.sub(r"\s+", " ", raw_role.strip())
    lower = role.lower()

    if lower.endswith("assistant coach"):
        return role[: -len("Assistant Coach")].strip(), "Assistant Coach"
    if lower.endswith("head coach"):
        return role[: -len("Head Coach")].strip(), "Head Coach"

    return role, "Unknown"


def get_rendered_html(url: str) -> str:
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # The source site appears to load rows asynchronously after initial paint.
            page.wait_for_timeout(6000)
            page.wait_for_load_state("networkidle", timeout=30000)

            try:
                page.wait_for_selector("tr td", timeout=15000)
            except PlaywrightTimeoutError:
                # Keep parsing whatever content is available.
                pass

            html = page.content()
            browser.close()
            return html
    except Exception:
        # Fallback for environments where Playwright/browser installation is unavailable.
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        return response.text


def fetch_cloud_data_items(url: str) -> List[dict]:
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
        page.wait_for_load_state("networkidle", timeout=30000)
        page.wait_for_timeout(3000)
        browser.close()

    return list(captured.values())


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


def write_summary_page(certified: Sequence[CoachRow], in_progress: Sequence[CoachRow], transitions: Sequence[dict]) -> None:
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
        <p class=\"meta\">As of {date.today()} | Certified: {len(certified)} | In Progress: {len(in_progress)}</p>

        <section class=\"card\">
            <h2>Detected Transitions</h2>
            {transition_html}
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

    certified = sort_rows(filter_sarnia(certified_all))
    in_progress = sort_rows(filter_sarnia(in_progress_all))

    previous = load_previous_status()
    prev_in_progress = as_rows(previous.get("inProgress", []))
    transitions = compute_transitions(prev_in_progress, certified)

    payload = {
        "asOf": str(date.today()),
        "sources": {
            "certified": CERTIFIED_URL,
            "inProgress": IN_PROGRESS_URL,
        },
        "certified": [asdict(r) for r in certified],
        "inProgress": [asdict(r) for r in in_progress],
        "transitions": transitions,
        "notes": [
            "Rows are filtered to associations containing 'Sarnia'.",
            "A transition is detected when a previously in-progress coach row appears in certified.",
        ],
    }

    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_summary_page(certified, in_progress, transitions)
    print(
        f"Updated {STATUS_PATH} with {len(certified)} certified and {len(in_progress)} in-progress rows. "
        f"Raw rows: certified={len(certified_all)}, in-progress={len(in_progress_all)}"
    )


if __name__ == "__main__":
    main()
