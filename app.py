import gzip
import os
import re
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
page_title="Dubai DLD Unit Finder",
page_icon="🏢",
layout="wide",
)

GITHUB_REPO = "r6393480-cpu/dld-unit-finder"
RELEASE_TAG = "latest-data"

DATABASE_URL = (
f"https://github.com/{GITHUB_REPO}/releases/download/"
f"{RELEASE_TAG}/dld_units.db.gz"
)

SQFT_PER_SQM = 10.7639104167
MAX_AREA_DIFFERENCE = 10.0
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
COPY_CHUNK_SIZE = 8 * 1024 * 1024

=========================================================

HELPERS

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
    return number if number > 0 else None
except (ValueError, TypeError):
    return None

def to_bedroom_count(value):
if value is None:
return None

value = str(value).strip().lower()
if value in ("studio", "studios"):
    return 0
match = re.search(r"\d+", value)
return int(match.group()) if match else None

def safe_get(row, column, default=None):
try:
return row[column]
except (KeyError, IndexError, TypeError):
return default

=========================================================

DATABASE DOWNLOAD AND EXTRACTION

=========================================================

@st.cache_resource(show_spinner=False)
def load_database():
data_dir = Path(tempfile.gettempdir()) / “dld_unit_finder”
data_dir.mkdir(parents=True, exist_ok=True)

db_path = data_dir / "dld_units.db"
gzip_path = data_dir / "dld_units.db.gz"
partial_path = data_dir / "dld_units.partial.db"
# Reuse the existing database if valid.
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
                WHERE type = 'table'
                AND name = 'units'
                """
            ).fetchone()
            if table:
                columns = [
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(units)"
                    )
                ]
                if columns:
                    return str(db_path), columns
    except sqlite3.Error:
        pass
    db_path.unlink(missing_ok=True)
# Download in chunks rather than loading 354 MB into RAM.
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
# Extract directly to disk in chunks.
try:
    with gzip.open(gzip_path, "rb") as source:
        with open(partial_path, "wb") as destination:
            shutil.copyfileobj(
                source,
                destination,
                length=COPY_CHUNK_SIZE,
            )
    # Validate the extracted SQLite database.
    with sqlite3.connect(
        f"file:{partial_path}?mode=ro",
        uri=True,
        timeout=30,
    ) as connection:
        table = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            AND name = 'units'
            """
        ).fetchone()
        if not table:
            raise RuntimeError(
                "The database does not contain a units table."
            )
        columns = [
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(units)"
            )
        ]
        if not columns:
            raise RuntimeError(
                "Could not read database columns."
            )
    os.replace(partial_path, db_path)
    gzip_path.unlink(missing_ok=True)
    return str(db_path), columns
except Exception as exc:
    partial_path.unlink(missing_ok=True)
    raise RuntimeError(
        f"Database extraction failed: {exc}"
    ) from exc

=========================================================

LISTING SCRAPER

=========================================================

def parse_area_from_text(text):
if not text:
return None

patterns = [
    (
        r"([\d,]+(?:\.\d+)?)\s*"
        r"(?:sq\.?\s*ft|sqft|sq\s*ft|square feet)",
        "sqft",
    ),
    (
        r"([\d,]+(?:\.\d+)?)\s*"
        r"(?:sq\.?\s*m|sqm|square metres|square meters)",
        "sqm",
    ),
]
for pattern, unit in patterns:
    match = re.search(
        pattern,
        text,
        flags=re.IGNORECASE,
    )
    if match:
        area = to_float(match.group(1))
        if area and unit == "sqm":
            area *= SQFT_PER_SQM
        return area
return None

def scrape_listing(url):
parsed = urlparse(url)

if parsed.scheme not in ("http", "https"):
    raise ValueError("Enter a valid listing URL.")
if not parsed.netloc.lower().endswith("propertyfinder.ae"):
    raise ValueError("Enter a Property Finder UAE URL.")
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
title = ""
og_title = soup.find("meta", property="og:title")
if og_title:
    title = og_title.get("content", "").strip()
if not title and soup.title:
    title = soup.title.get_text(" ", strip=True)
page_text = soup.get_text(" ", strip=True)
area = parse_area_from_text(page_text)
if area is None:
    area = parse_area_from_text(title)
bedrooms = None
bedroom_match = re.search(
    r"\b(studio|\d+)\s*(?:bedroom|bedrooms|bed)\b",
    title + " " + page_text[:12000],
    flags=re.IGNORECASE,
)
if bedroom_match:
    bedrooms = to_bedroom_count(
        bedroom_match.group(1)
    )
project = ""
project_match = re.search(
    r"\b(?:sale|rent)\s+in\s+([^:|]+)",
    title,
    flags=re.IGNORECASE,
)
if project_match:
    project = project_match.group(1).strip()
return {
    "title": title,
    "project": project,
    "area": area,
    "bedrooms": bedrooms,
}

=========================================================

DLD AREA CONVERSION

=========================================================

def get_dld_area_sqft(row, columns):
sqft_fields = [
“area_sqft”,
“actual_area_sqft”,
“unit_area_sqft”,
“property_area_sqft”,
“unit_size_sqft”,
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

def project_matches(requested, actual):
requested = clean_text(requested)
actual = clean_text(actual)

return bool(requested) and requested == actual

=========================================================

MATCHING

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
        "Missing database columns: "
        + ", ".join(sorted(missing))
    )
if not project.strip():
    raise ValueError("Project name is required.")
if listing_area <= 0:
    raise ValueError("Enter a valid listing area.")
query = """
    SELECT *
    FROM units
    WHERE lower(trim(project_name_en)) LIKE ?
"""
best = None
records_checked = 0
connection = sqlite3.connect(
    f"file:{db_path}?mode=ro",
    uri=True,
    timeout=60,
)
connection.row_factory = sqlite3.Row
try:
    cursor = connection.execute(
        query,
        ("%" + project.strip().lower() + "%",),
    )
    # Iterate rows without fetchall().
    for row in cursor:
        actual_project = safe_get(
            row,
            "project_name_en",
        )
        if not project_matches(project, actual_project):
            continue
        records_checked += 1
        area_sqft = get_dld_area_sqft(row, columns)
        if area_sqft is None:
            continue
        difference = abs(area_sqft - listing_area)
        if difference > MAX_AREA_DIFFERENCE:
            continue
        dld_bedrooms = None
        for field in (
            "rooms",
            "bedrooms",
            "number_of_bedrooms",
        ):
            if field in columns:
                dld_bedrooms = to_bedroom_count(
                    safe_get(row, field)
                )
                if dld_bedrooms is not None:
                    break
        bedroom_match = (
            bedrooms is not None
            and dld_bedrooms is not None
            and bedrooms == dld_bedrooms
        )
        candidate = {
            "unit_number": safe_get(row, "unit_number"),
            "project": actual_project,
            "area_sqft": area_sqft,
            "area_difference": difference,
            "bedrooms": dld_bedrooms,
            "bedroom_match": bedroom_match,
            "property_type": safe_get(
                row,
                "property_sub_type_en",
                "",
            ),
        }
        if best is None:
            best = candidate
        elif difference < best["area_difference"]:
            best = candidate
        elif (
            difference == best["area_difference"]
            and bedroom_match
            and not best["bedroom_match"]
        ):
            best = candidate
finally:
    connection.close()
return best, records_checked

=========================================================

STREAMLIT INTERFACE

=========================================================

st.title(“Dubai DLD Unit Finder”)

st.caption(
“Find a potential DLD unit match using a Property Finder listing.”
)

st.info(
“Project name is mandatory. Area is the main matching criterion. “
“Candidates more than 10 sqft away are rejected.”
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
st.error(“Please enter a listing URL.”)
else:
try:
with st.spinner(“Reading listing details…”):
details = scrape_listing(
listing_url.strip()
)

        st.session_state["listing_url"] = listing_url.strip()
        st.session_state["scraped_listing"] = details
    except Exception as exc:
        st.error(f"Could not read listing: {exc}")

details = st.session_state.get(“scraped_listing”)

if details:
st.subheader(“Listing details”)

if details.get("title"):
    st.write("**Listing title:**", details["title"])
with st.form("matching_form"):
    project = st.text_input(
        "Project name (required)",
        value=details.get("project") or "",
    )
    area = st.number_input(
        "Listing area (sqft)",
        min_value=0.0,
        value=float(details.get("area") or 0.0),
        step=0.01,
        format="%.2f",
    )
    options = [
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
    scraped_bedrooms = details.get("bedrooms")
    default_bedroom = "Not specified"
    if scraped_bedrooms == 0:
        default_bedroom = "Studio"
    elif scraped_bedrooms is not None:
        default_bedroom = str(scraped_bedrooms)
    index = (
        options.index(default_bedroom)
        if default_bedroom in options
        else 0
    )
    bedroom_selection = st.selectbox(
        "Bedrooms",
        options,
        index=index,
    )
    run_matching = st.form_submit_button(
        "Run Matching",
        type="primary",
    )
if run_matching:
    if not project.strip():
        st.error("Project name is mandatory.")
    elif area <= 0:
        st.error("Enter the correct listing area in sqft.")
    else:
        selected_bedrooms = None
        if bedroom_selection == "Studio":
            selected_bedrooms = 0
        elif bedroom_selection != "Not specified":
            selected_bedrooms = int(bedroom_selection)
        try:
            with st.spinner(
                "Loading database and matching units..."
            ):
                db_path, columns = load_database()
                best, checked = find_best_match(
                    db_path=db_path,
                    columns=columns,
                    project=project.strip(),
                    listing_area=float(area),
                    bedrooms=selected_bedrooms,
                )
            st.subheader("Matching result")
            if best is None:
                st.warning(
                    "No sufficiently close candidate was found. "
                    "No unit number has been selected."
                )
                st.caption(
                    f"Project records checked: {checked}. "
                    f"Maximum area difference: "
                    f"{MAX_AREA_DIFFERENCE:.0f} sqft."
                )
            else:
                st.success("Closest matching candidate found.")
                col1, col2 = st.columns(2)
                with col1:
                    st.metric(
                        "Potential DLD unit number",
                        str(best["unit_number"]),
                    )
                with col2:
                    st.metric(
                        "Area difference",
                        f'{best["area_difference"]:.2f} sqft',
                    )
                st.write("**Project:**", best["project"])
                st.write(
                    "**Listing area:**",
                    f"{area:.2f} sqft",
                )
                st.write(
                    "**DLD area:**",
                    f'{best["area_sqft"]:.2f} sqft',
                )
                if best["bedrooms"] is not None:
                    st.write(
                        "**DLD rooms/bedrooms field:**",
                        best["bedrooms"],
                    )
                if best["property_type"]:
                    st.write(
                        "**Property subtype:**",
                        best["property_type"],
                    )
                st.caption(
                    "This is a potential match, not proof of "
                    "unit identity. Verify it against the "
                    "official listing or DLD record."
                )
        except Exception as exc:
            st.error(f"Error running matching: {exc}")
            st.info(
                "The database expands to approximately 1.5 GB. "
                "If the hosting environment lacks sufficient disk "
                "space, this code alone cannot resolve that limit."
            )

st.divider()

st.caption(
“Area matching uses a 10 sqft tolerance. “
“A close area alone cannot guarantee the correct unit.”
)
