import json
import os
import re
from dataclasses import dataclass, asdict
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
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except PlaywrightTimeoutError:
            pass
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
