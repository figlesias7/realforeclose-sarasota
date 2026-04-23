import asyncio
from playwright.async_api import async_playwright
import csv
import json
import os
import re
from datetime import datetime
from html import escape

BASE_DOMAIN = "https://sarasota.realforeclose.com/"
CALENDAR_URL = f"{BASE_DOMAIN}/index.cfm?zaction=USER&zmethod=CALENDAR"

DATA_DIR = "data"
DOCS_DIR = "docs"
TODAY_STR = datetime.now().strftime("%Y-%m-%d")
TODAY_FILE = os.path.join(DATA_DIR, f"{TODAY_STR}.csv")
SEEN_FILE = os.path.join(DATA_DIR, "all_seen.csv")
INDEX_FILE = os.path.join(DATA_DIR, "index.json")
HTML_FILE = os.path.join(DOCS_DIR, "index.html")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(DOCS_DIR, exist_ok=True)


def clean_text(value: str) -> str:
    return " ".join(str(value).replace("\xa0", " ").split())


def load_seen() -> set[str]:
    if not os.path.exists(SEEN_FILE):
        return set()

    seen = set()
    with open(SEEN_FILE, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if row and row[0].strip():
                seen.add(row[0].strip())
    return seen


def save_seen(seen: set[str]) -> None:
    with open(SEEN_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for case_no in sorted(seen):
            writer.writerow([case_no])


def update_index() -> list[str]:
    files = sorted(
        [f for f in os.listdir(DATA_DIR) if f.endswith(".csv") and f != "all_seen.csv"],
        reverse=True,
    )
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(files, f)
    return files


def extract_auctions_waiting(text: str) -> str:
    start = text.find("Auctions Waiting")
    if start == -1:
        return ""

    section = text[start:]

    stop_markers = [
        "Auctions Closed",
        "Closed Auctions",
        "Canceled Auctions",
        "Auctions Canceled",
        "Sales List",
        "Connection",
        "About Us | Site Map |",
    ]

    end_positions = [section.find(marker) for marker in stop_markers if section.find(marker) != -1]
    if end_positions:
        section = section[:min(end_positions)]

    return section


def parse_waiting_records(section_text: str) -> list[dict]:
    if not section_text:
        return []

    date_pattern = re.compile(
        r"Auction Starts\s*:?\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2}\s*[AP]M(?:\s*ET)?)?)",
        re.IGNORECASE,
    )

    case_starts = list(re.finditer(r"Case\s*#\s*:", section_text, re.IGNORECASE))
    if not case_starts:
        return []

    rows = []
    current_auction_date = ""

    for i, case_match in enumerate(case_starts):
        start = case_match.start()
        end = case_starts[i + 1].start() if i + 1 < len(case_starts) else len(section_text)

        prefix_start = case_starts[i - 1].end() if i > 0 else 0
        prefix = section_text[prefix_start:start]
        date_match = list(date_pattern.finditer(prefix))
        if date_match:
            current_auction_date = clean_text(date_match[-1].group(1))

        block = clean_text(section_text[start:end])
        if not block:
            continue

        def grab(pattern: str, default: str = "") -> str:
            m = re.search(pattern, block, re.IGNORECASE | re.DOTALL)
            return clean_text(m.group(1)) if m else default

        case_no = grab(
            r"Case\s*#\s*:\s*(.*?)(?=Final Judgment Amount:|Parcel ID:|Property Address:|Assessed Value:|Plaintiff Max Bid:|Auction Type:|$)"
        )
        if not case_no:
            continue

        auction_date = grab(
            r"Auction Starts\s*:?\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4}(?:\s+[0-9]{1,2}:[0-9]{2}\s*[AP]M(?:\s*ET)?)?)",
            current_auction_date,
        ) or current_auction_date

        judgment = grab(r"Final Judgment Amount\s*:?\s*(\$[\d,]+\.\d{2}|Hidden|\$0\.00|0\.00)")
        parcel_id = grab(r"Parcel ID\s*:?\s*([^\s]+)")
        address = grab(
            r"Property Address\s*:?\s*(.*?)(?=Assessed Value:|Plaintiff Max Bid:|Auction Type:|Case\s*#:|Final Judgment Amount:|Parcel ID:|$)"
        )
        assessed = grab(r"Assessed Value\s*:?\s*(\$[\d,]+\.\d{2}|Hidden|\$0\.00|0\.00)")
        max_bid = grab(r"Plaintiff Max Bid\s*:?\s*(\$[\d,]+\.\d{2}|Hidden|\$0\.00|0\.00)")

        rows.append({
            "Auction Date": auction_date,
            "Property Address": address,
            "Final Judgment": judgment,
            "Assessed Value": assessed,
            "Plaintiff Max Bid": max_bid,
            "Case #": case_no,
            "Parcel ID": parcel_id,
            "Case Link": f"{BASE_DOMAIN}/index.cfm?zaction=auction&zmethod=details&AID={case_no}&bypassPage=1",
            "Parcel Link": f"https://qpublic.schneidercorp.com/Application.aspx?AppID=856&LayerID=16069&PageTypeID=4&KeyValue={parcel_id}" if parcel_id else "",
        })

    return rows


def write_daily(rows: list[dict]) -> None:
    with open(TODAY_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Auction Date",
            "Property Address",
            "Final Judgment",
            "Assessed Value",
            "Plaintiff Max Bid",
            "Case #",
            "Parcel ID",
            "Case Link",
            "Parcel Link",
        ])
        for r in rows:
            writer.writerow([
                r["Auction Date"],
                r["Property Address"],
                r["Final Judgment"],
                r["Assessed Value"],
                r["Plaintiff Max Bid"],
                r["Case #"],
                r["Parcel ID"],
                r["Case Link"],
                r["Parcel Link"],
            ])


def read_csv_rows(path: str) -> list[dict]:
    rows = []
    if not os.path.exists(path):
        return rows

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(dict(row))
    return rows


def build_html(index_files: list[str]) -> None:
    list_items = []
    sections = []

    for i, file in enumerate(index_files):
        date_label = file.replace(".csv", "")
        section_id = f"day-{date_label}"
        active_class = "active" if i == 0 else ""

        list_items.append(
            f'<li><a href="#{section_id}" class="{active_class}">{escape(date_label)}</a></li>'
        )

        rows = read_csv_rows(os.path.join(DATA_DIR, file))
        if rows:
            body_rows = "\n".join(
                f"""
                <tr>
                  <td>{escape(r.get("Auction Date", ""))}</td>
                  <td>{escape(r.get("Property Address", ""))}</td>
                  <td>{escape(r.get("Final Judgment", ""))}</td>
                  <td>{escape(r.get("Assessed Value", ""))}</td>
                  <td>{escape(r.get("Plaintiff Max Bid", ""))}</td>
                  <td><a href="{escape(r.get("Case Link", ""))}" target="_blank">{escape(r.get("Case #", ""))}</a></td>
                  <td><a href="{escape(r.get("Parcel Link", ""))}" target="_blank">{escape(r.get("Parcel ID", ""))}</a></td>
                </tr>
                """
                for r in rows
            )
        else:
            body_rows = '<tr><td colspan="7">No records</td></tr>'

        sections.append(
            f"""
            <section id="{section_id}" class="day-section">
              <h2>{escape(date_label)}</h2>
              <table>
                <thead>
                  <tr>
                    <th>Auction Date</th>
                    <th>Property Address</th>
                    <th>Final Judgment</th>
                    <th>Assessed Value</th>
                    <th>Plaintiff Max Bid</th>
                    <th>Case #</th>
                    <th>Parcel ID</th>
                  </tr>
                </thead>
                <tbody>
                  {body_rows}
                </tbody>
              </table>
            </section>
            """
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Daily New Foreclosures</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 20px; }}
    h1 {{ margin-bottom: 16px; }}
    ul {{ padding-left: 20px; }}
    li {{ margin-bottom: 6px; }}
    a {{ color: #0645ad; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 10px; margin-bottom: 28px; }}
    th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f3f3f3; }}
  </style>
</head>
<body>
  <h1>Daily New Foreclosures</h1>
  <ul>
    {''.join(list_items) if list_items else '<li>No data files yet</li>'}
  </ul>
  {''.join(sections) if sections else '<p>No data files yet.</p>'}
</body>
</html>
"""
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)


async def get_month_info(page) -> tuple[list[dict], str | None]:
    boxes = await page.locator(".CALBOX").all()
    days = []

    for idx in range(len(boxes)):
        try:
            box = page.locator(".CALBOX").nth(idx)
            text = clean_text(await box.inner_text(timeout=3000))
        except Exception:
            continue

        if "Foreclosure" not in text or "FC" not in text:
            continue

        m = re.search(r"^(\d+).*?(\d+)\s*/\s*(\d+)\s*FC", text)
        if not m:
            continue

        day_num = int(m.group(1))
        active = int(m.group(2))
        scheduled = int(m.group(3))

        if active <= 0:
            continue

        days.append({
            "index": idx,
            "day": day_num,
            "active": active,
            "scheduled": scheduled,
        })

    next_month_url = None
    links = await page.locator("a").evaluate_all(
        """
        els => els.map(a => ({
            text: (a.innerText || a.textContent || '').trim(),
            href: a.href || ''
        }))
        """
    )
    candidates = []
    for link in links:
        href = link["href"]
        if "zmethod=calendar" in href.lower() and "selCalDate=" in href:
            candidates.append(href)

    if candidates:
        next_month_url = candidates[-1]

    return days, next_month_url


async def scrape():
    seen_cases = load_seen()
    all_rows_for_today = []
    visited_months = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        current_month_url = CALENDAR_URL
        empty_month_streak = 0

        while current_month_url and current_month_url not in visited_months:
            visited_months.add(current_month_url)

            await page.goto(current_month_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)

            days, next_month_url = await get_month_info(page)
            print(f"Found {len(days)} live foreclosure days in {current_month_url}")

            if not days:
                empty_month_streak += 1
                if empty_month_streak >= 1:
                    break
            else:
                empty_month_streak = 0

            for item in days:
                try:
                    print(f"Opening day {item['day']} with {item['active']} live auctions")

                    await page.goto(current_month_url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2500)

                    await page.locator(".CALBOX").nth(item["index"]).click(timeout=5000, force=True)
                    await page.wait_for_timeout(5000)

                    body_text = await page.locator("body").inner_text()
                    waiting_text = extract_auctions_waiting(body_text)
                    rows = parse_waiting_records(waiting_text)

                    print(f"  Parsed {len(rows)} waiting records")
                    if not rows:
                        print("  DEBUG waiting text preview:", waiting_text[:1500])

                    for r in rows:
                        case_no = r["Case #"]
                        if not case_no:
                            continue
                        all_rows_for_today.append(r)
                        seen_cases.add(case_no)

                except Exception as e:
                    print(f"skip day {item['day']} error: {e}")
                    continue

            current_month_url = next_month_url

        await browser.close()

    write_daily(all_rows_for_today)
    save_seen(seen_cases)
    index_files = update_index()
    build_html(index_files)
    print(f"Saved {len(all_rows_for_today)} records for today")


if __name__ == "__main__":
    asyncio.run(scrape())