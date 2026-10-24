import os
import re
import gzip
import json
import shutil
import sqlite3
import tempfile
import requests
import streamlit as st

from pathlib import Path
from urllib.parse import urlparse
from bs4 import BeautifulSoup

=========================================================

CONFIGURATION

=========================================================

st.set_page_config(
page_title=“Dubai DLD Unit Finder”,
page_icon=“🏢”,
layout=“wide”,
)

GITHUB_REPO = “r6393480-cpu/dld-unit-finder”
RELEASE_TAG = “latest-data”

DATABASE_URL = (
f”https://github.com/{GITHUB_REPO}/releases/download/”
f”{RELEASE_TAG}/dld_units.db.gz”
)

SQFT_PER_SQM = 10.7639104167
MAX_AREA_DIFFERENCE = 10.0
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
COPY_CHUNK_SIZE = 8 * 1024 * 1024

=========================================================

TEXT / NUMBER HELPERS

=========================================================

def clean_text(value):
if value is None:
return “”

value = str(value).strip().lower()
value = re.sub(r"[^a-z0-9]+", " ", value)
return re.sub(r"\s+", " ", value).strip()

def to_float(value):
if value is None:
return None

try:
    value = str(value).replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    if not match:
        return None
    number = float(match.group())
    if number <= 0:
        return None
    return number
except (ValueError, TypeError):
    return None

def to_bedroom_count(value):
if value is None:
return None

value = str(value).strip().lower()
if value in {"studio", "studios"}:
    return 0
match = re.search(r"\d+", value)
if match:
    return int(match.group())
return None

def safe_get(row, column, default=None):
try:
return row[column]
except (KeyError, IndexError, TypeError):
return default

=========================================================

DATABASE DOWNLOAD

STREAMING DOWNLOAD + STREAMING DECOMPRESSION

=========================================================

@st.cache_resource(show_spinner=False)
def load_database():
“””
Downloads the compressed database in chunks and decompresses
directly to disk. It does not load the complete database
into Python memory.
“””

data_dir = Path(tempfile.gettempdir()) / "dld_unit_finder"
data_dir.mkdir(parents=True, exist_ok=True)
db_path = data_dir / "dld_units.db"
gzip_path = data_dir / "dld_units.db.gz"
# Reuse an existing valid database after a Streamlit rerun.
if db_path.exists() and db_path.stat().st_size > 0:
    try:
        with sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            timeout=30,
        ) as connection:
            table = connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name = 'units'
                """
            ).fetchone()
            if table:
                columns = [
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(units)"
                    ).fetchall()
                ]
                if columns:
                    return str(db_path), columns
    except sqlite3.Error:
        pass
    # Remove an incomplete or invalid database.
    try:
        db_path.unlink(missing_ok=True)
    except OSError:
        pass
# Download the compressed file to disk in small chunks.
try:
    with requests.get(
        DATABASE_URL,
        stream=True,
        timeout=(30, 180),
        headers={"User-Agent": "DLD-Unit-Finder/1.0"},
    ) as response:
        response.raise_for_status()
        with open(gzip_path, "wb") as output:
            for chunk in response.iter_content(
                chunk_size=DOWNLOAD_CHUNK_SIZE
            ):
                if chunk:
                    output.write(chunk)
except Exception as exc:
    gzip_path.unlink(missing_ok=True)
    raise RuntimeError(
        f"Database download failed: {exc}"
    ) from exc
# Decompress to disk without keeping the entire 1.5 GB
# database in memory.
temp_db_path = data_dir / "dld_units.partial.db"
try:
    with gzip.open(gzip_path, "rb") as source:
        with open(temp_db_path, "wb") as destination:
            shutil.copyfileobj(
                source,
                destination,
                length=COPY_CHUNK_SIZE,
            )
    # Validate the extracted SQLite database.
    with sqlite3.connect(
        f"file:{temp_db_path}?mode=ro",
        uri=True,
        timeout=30,
    ) as connection:
        table = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'units'
            """
        ).fetchone()
        if not table:
            raise RuntimeError(
                "The downloaded database has no 'units' table."
            )
        columns = [
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(units)"
            ).fetchall()
        ]
        if not columns:
            raise RuntimeError(
                "Could not read the database columns."
            )
    os.replace(temp_db_path, db_path)
    gzip_path.unlink(missing_ok=True)
    return str(db_path), columns
except Exception as exc:
    temp_db_path.unlink(missing_ok=True)
    raise RuntimeError(
        f"Database extraction/validation failed: {exc}"
    ) from exc

=========================================================

PROPERTY FINDER LISTING SCRAPER

=========================================================

def parse_area_from_text(text):
if not text:
return None

patterns = [
    r"([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|sq\s*ft|square feet)",
    r"([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*m|sqm|square metres|square meters)",
]
for index, pattern in enumerate(patterns):
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if match:
        area = to_float(match.group(1))
        if area and index == 1:
            area *= SQFT_PER_SQM
        return area
return None

def parse_listing_json(data, results):
if isinstance(data, dict):
for key, value in data.items():
key_lower = str(key).lower()

        if isinstance(value, (str, int, float)):
            if key_lower in {
                "name",
                "headline",
                "title",
            }:
                results.setdefault("title", str(value))
            elif key_lower in {
                "bedrooms",
                "bedroom",
                "numberofbedrooms",
            }:
                results.setdefault("bedrooms", to_bedroom_count(value))
            elif key_lower in {
                "floorarea",
                "area",
                "size",
                "builtuparea",
                "built_up_area",
            }:
                area = to_float(value)
                if area:
                    results.setdefault("area", area)
            elif key_lower in {
                "projectname",
                "project_name",
            }:
                results.setdefault("project", str(value))
        elif isinstance(value, (dict, list)):
            parse_listing_json(value, results)
elif isinstance(data, list):
    for item in data:
        parse_listing_json(item, results)

def scrape_listing(url):
parsed = urlparse(url)

if parsed.scheme not in {"http", "https"}:
    raise ValueError("Enter a valid Property Finder listing URL.")
if not parsed.netloc.lower().endswith("propertyfinder.ae"):
    raise ValueError("Please enter a Property Finder UAE URL.")
response = requests.get(
    url,
    headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    },
    timeout=(15, 40),
)
response.raise_for_status()
soup = BeautifulSoup(response.text, "html.parser")
details = {
    "title": "",
    "project": "",
    "area": None,
    "bedrooms": None,
}
title_tag = soup.find("meta", property="og:title")
if title_tag:
    details["title"] = title_tag.get("content", "").strip()
if not details["title"] and soup.title:
    details["title"] = soup.title.get_text(" ", strip=True)
# Read embedded JSON-LD where available.
for script in soup.find_all(
    "script",
    attrs={"type": "application/ld+json"},
):
    try:
        data = json.loads(script.string or script.get_text())
        parse_listing_json(data, details)
    except (ValueError, TypeError):
        continue
page_text = soup.get_text(" ", strip=True)
if details["area"] is None:
    details["area"] = parse_area_from_text(page_text)
# Try to identify bedrooms from the page text/title.
if details["bedrooms"] is None:
    text_to_check = f"{details['title']} {page_text[:12000]}"
    bedroom_match = re.search(
        r"\b(studio|\d+)\s*(?:bedroom|bedrooms|bed)\b",
        text_to_check,
        flags=re.IGNORECASE,
    )
    if bedroom_match:
        details["bedrooms"] = to_bedroom_count(
            bedroom_match.group(1)
        )
# Extract project from common listing-title format.
# Example: "Sale in Serenova: GENUINE RESALE ..."
if not details["project"]:
    title = details["title"]
    project_match = re.search(
        r"\b(?:sale|rent)\s+in\s+([^:|]+)",
        title,
        flags=re.IGNORECASE,
    )
    if project_match:
        details["project"] = project_match.group(1).strip()
return details

=========================================================

DLD AREA CONVERSION

=========================================================

def get_dld_area_sqft(row, columns):
“””
Prefer a field explicitly labelled as square feet.
Otherwise use actual_area as square metres, as indicated
by the database test case.
“””

sqft_fields = [
    "area_sqft",
    "actual_area_sqft",
    "unit_area_sqft",
    "property_area_sqft",
    "unit_size_sqft",
]
for field in sqft_fields:
    if field in columns:
        value = to_float(safe_get(row, field))
        if value:
            return value
sqm_fields = [
    "actual_area",
    "unit_area",
    "property_area",
    "area",
]
for field in sqm_fields:
    if field in columns:
        value = to_float(safe_get(row, field))
        if value:
            return value * SQFT_PER_SQM
return None

=========================================================

PROJECT MATCHING

=========================================================

def project_matches(requested_project, database_project):
requested = clean_text(requested_project)
actual = clean_text(database_project)

if not requested or not actual:
    return False
# Mandatory project match; avoid unrelated projects.
return requested == actual

=========================================================

FIND THE BEST UNIT

Processes database rows incrementally; no fetchall().

=========================================================

def find_best_match(
db_path,
columns,
project,
listing_area,
bedrooms=None,
):
required_columns = {
“project_name_en”,
“unit_number”,
}

missing = required_columns.difference(columns)
if missing:
    raise RuntimeError(
        "Database is missing required columns: "
        + ", ".join(sorted(missing))
    )
if not project.strip():
    raise ValueError("Project name is required.")
if listing_area is None or listing_area <= 0:
    raise ValueError("Enter a valid listing area in sqft.")
query = """
    SELECT *
    FROM units
    WHERE lower(trim(project_name_en)) LIKE ?
"""
search_pattern = "%" + project.strip().lower() + "%"
best = None
candidates_checked = 0
connection = sqlite3.connect(
    f"file:{db_path}?mode=ro",
    uri=True,
    timeout=60,
)
connection.row_factory = sqlite3.Row
try:
    cursor = connection.execute(query, (search_pattern,))
    # Iterate through the SQLite cursor rather than loading
    # every row into RAM.
    for row in cursor:
        database_project = safe_get(row, "project_name_en")
        if not project_matches(project, database_project):
            continue
        candidates_checked += 1
        area_sqft = get_dld_area_sqft(row, columns)
        if area_sqft is None:
            continue
        area_difference = abs(area_sqft - listing_area)
        # Do not return a candidate whose area is too far away.
        if area_difference > MAX_AREA_DIFFERENCE:
            continue
        database_bedrooms = None
        for bedroom_column in (
            "rooms",
            "bedrooms",
            "number_of_bedrooms",
        ):
            if bedroom_column in columns:
                database_bedrooms = to_bedroom_count(
                    safe_get(row, bedroom_column)
                )
                if database_bedrooms is not None:
                    break
        bedroom_match = (
            bedrooms is not None
            and database_bedrooms is not None
            and bedrooms == database_bedrooms
        )
        subtype = safe_get(
            row,
            "property_sub_type_en",
            "",
        )
        # Area is the primary criterion.
        # Bedroom agreement is only a tie-breaker.
        candidate = {
            "unit_number": safe_get(row, "unit_number"),
            "project": database_project,
            "area_sqft": area_sqft,
            "area_difference": area_difference,
            "bedrooms": database_bedrooms,
            "bedroom_match": bedroom_match,
            "property_type": subtype,
        }
        if best is None:
            best = candidate
        elif candidate["area_difference"] < best["area_difference"]:
            best = candidate
        elif (
            candidate["area_difference"] == best["area_difference"]
            and candidate["bedroom_match"]
            and not best["bedroom_match"]
        ):
            best = candidate
finally:
    connection.close()
return best, candidates_checked

=========================================================

STREAMLIT UI

=========================================================

st.title(“Dubai DLD Unit Finder”)
st.caption(
“Match a Property Finder listing against the DLD database.”
)

st.info(
“The app prioritizes project name and the closest area. “
“It will not select a unit if no candidate is within “
f”{MAX_AREA_DIFFERENCE:.0f} sqft of the listing area.”
)

with st.form(“listing_form”):
listing_url = st.text_input(
“Property Finder listing URL”,
placeholder=“https://www.propertyfinder.ae/en/plp/…”,
)

read_listing = st.form_submit_button(
    "Read Listing Details",
    type="primary",
)

if read_listing:
if not listing_url.strip():
st.error(“Please enter a Property Finder listing URL.”)
else:
try:
with st.spinner(“Reading listing details…”):
scraped = scrape_listing(listing_url.strip())

        st.session_state["listing_url"] = listing_url.strip()
        st.session_state["scraped_listing"] = scraped
        st.success("Listing page processed.")
    except Exception as exc:
        st.error(f"Could not read the listing: {exc}")

scraped = st.session_state.get(“scraped_listing”, {})

if scraped:
st.subheader(“Listing details”)

with st.form("matching_form"):
    project = st.text_input(
        "Project name (required)",
        value=scraped.get("project") or "",
    )
    default_area = scraped.get("area")
    area_input = st.number_input(
        "Listing area (sqft)",
        min_value=0.0,
        value=float(default_area or 0.0),
        step=1.0,
        format="%.2f",
    )
    scraped_bedrooms = scraped.get("bedrooms")
    bedroom_options = [
        "Not specified",
        "Studio",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
    ]
    default_bedroom_index = 0
    if scraped_bedrooms is not None:
        bedroom_label = (
            "Studio"
            if scraped_bedrooms == 0
            else str(scraped_bedrooms)
        )
        if bedroom_label in bedroom_options:
            default_bedroom_index = bedroom_options.index(
                bedroom_label
            )
    bedroom_selection = st.selectbox(
        "Bedrooms",
        options=bedroom_options,
        index=default_bedroom_index,
    )
    run_matching = st.form_submit_button(
        "Run Matching",
        type="primary",
    )
if run_matching:
    if not project.strip():
        st.error("Project name is mandatory.")
    elif area_input <= 0:
        st.error("Enter the correct listing area in sqft.")
    else:
        if bedroom_selection == "Not specified":
            selected_bedrooms = None
        elif bedroom_selection == "Studio":
            selected_bedrooms = 0
        else:
            selected_bedrooms = int(bedroom_selection)
        try:
            with st.spinner(
                "Preparing database and matching units..."
            ):
                db_path, db_columns = load_database()
                best, checked = find_best_match(
                    db_path=db_path,
                    columns=db_columns,
                    project=project.strip(),
                    listing_area=float(area_input),
                    bedrooms=selected_bedrooms,
                )
            st.subheader("Matching result")
            if best is None:
                st.warning(
                    "No sufficiently close match was found for "
                    "this project and area. No unit number selected."
                )
                st.caption(
                    f"Project records checked: {checked}. "
                    f"Maximum allowed area difference: "
                    f"{MAX_AREA_DIFFERENCE:.0f} sqft."
                )
            else:
                st.success("Best matching candidate found.")
                col1, col2 = st.columns(2)
                with col1:
                    st.metric(
                        "DLD Unit Number",
                        str(best["unit_number"]),
                    )
                with col2:
                    st.metric(
                        "Area difference",
                        f'{best["area_difference"]:.2f} sqft',
                    )
                st.write(
                    "**Project:**",
                    best["project"],
                )
                st.write(
                    "**Listing area:**",
                    f"{area_input:.2f} sqft",
                )
                st.write(
                    "**DLD area:**",
                    f'{best["area_sqft"]:.2f} sqft',
                )
                if best["bedrooms"] is not None:
                    st.write(
                        "**DLD bedrooms/rooms field:**",
                        best["bedrooms"],
                    )
                if best["property_type"]:
                    st.write(
                        "**Property subtype:**",
                        best["property_type"],
                    )
                st.caption(
                    "This is the closest candidate under the "
                    "configured area threshold, not a guarantee "
                    "of the unit's identity. Verify against the "
                    "official listing or DLD record."
                )
        except Exception as exc:
            st.error(f"Error running matching: {exc}")
            st.caption(
                "If the error mentions disk space, the hosting "
                "environment may not have enough storage for the "
                "1.5 GB uncompressed database."
            )

st.divider()
st.caption(“DLD database is loaded locally for matching.”)
