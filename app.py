
import gzip
import os
import re
import sqlite3
import tempfile
from urllib.parse import urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup

SQFT_PER_SQM = 10.7639104167
AREA_LIMIT_SQFT = 10.0


st.set_page_config(page_title="Dubai DLD Unit Finder", page_icon="🏢")
st.title("Dubai DLD Unit Finder")
st.write("Find the closest DLD unit match from a Property Finder listing.")


def clean_text(value):
    value = str(value or "").lower().strip()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def number(value):
    if value is None:
        return None
    try:
        result = float(str(value).replace(",", "").strip())
        return result if result > 0 else None
    except (ValueError, TypeError):
        return None


def get_repo():
    try:
        repo = st.secrets.get("GITHUB_REPO", "")
    except Exception:
        repo = ""
    return repo or os.getenv("GITHUB_REPO", "r6393480-cpu/dld-unit-finder")


@st.cache_resource(show_spinner="Loading DLD database...")
def load_database():
    repo = get_repo()
    url = f"https://github.com/{repo}/releases/download/latest-data/dld_units.db.gz"

    response = requests.get(
        url,
        timeout=180,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    response.raise_for_status()

    db_bytes = gzip.decompress(response.content)

    db_path = os.path.join(tempfile.gettempdir(), "dld_units.db")
    with open(db_path, "wb") as file:
        file.write(db_bytes)

    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row

    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()

    table_names = [row["name"] for row in tables]
    if "units" not in table_names:
        connection.close()
        raise RuntimeError(
            f"'units' table not found. Available tables: {table_names}"
        )

    columns = [
        row["name"]
        for row in connection.execute("PRAGMA table_info(units)").fetchall()
    ]

    if "project_name_en" not in columns:
        connection.close()
        raise RuntimeError("'project_name_en' column not found in units table.")

    return db_path, columns


def scrape_listing(url):
    response = requests.get(
        url,
        timeout=30,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
            )
        }
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    title_tag = soup.find("title")
    title = title_tag.get_text(" ", strip=True) if title_tag else ""

    page_text = soup.get_text(" ", strip=True)

    # Property Finder title format commonly includes "Sale in PROJECT:"
    project = ""
    match = re.search(
        r"(?:sale|rent)\s+in\s+([^:|]+)",
        title,
        re.IGNORECASE
    )
    if match:
        project = match.group(1).strip()

    # Try common metadata fields if the title did not reveal the project.
    if not project:
        for tag in soup.find_all("meta"):
            content = tag.get("content", "")
            if tag.get("property") == "og:title" or tag.get("name") == "title":
                match = re.search(
                    r"(?:sale|rent)\s+in\s+([^:|]+)",
                    content,
                    re.IGNORECASE
                )
                if match:
                    project = match.group(1).strip()
                    break

    bedrooms = None
    bed_match = re.search(
        r"\b(\d+)\s*(?:bedrooms?|beds?|b/?r)\b",
        page_text,
        re.IGNORECASE
    )
    if bed_match:
        bedrooms = int(bed_match.group(1))

    area_sqft = None
    area_patterns = [
        r"([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|ft²|square feet)\b",
        r"([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*m|sqm|m²|square metres?)\b",
    ]

    for pattern in area_patterns:
        area_match = re.search(pattern, page_text, re.IGNORECASE)
        if area_match:
            area_value = number(area_match.group(1))
            if area_value:
                if re.search(r"sq\.?\s*m|sqm|m²|square metres?", pattern,
                             re.IGNORECASE):
                    area_sqft = area_value * SQFT_PER_SQM
                else:
                    area_sqft = area_value
                break

    return {
        "title": title,
        "project": project,
        "bedrooms": bedrooms,
        "area_sqft": area_sqft,
    }


def dld_area_sqft(row, columns):
    """
    actual_area in this dataset is in square metres.
    Prefer an explicitly named sqft field if the database has one.
    Do not add balcony area to actual_area.
    """
    for col in ("area_sqft", "unit_area_sqft", "actual_area_sqft"):
        if col in columns:
            value = number(row[col])
            if value is not None:
                return value

    if "actual_area" in columns:
        value = number(row["actual_area"])
        if value is not None:
            return value * SQFT_PER_SQM

    return None


def project_matches(requested_project, dld_project):
    requested = clean_text(requested_project)
    actual = clean_text(dld_project)

    if not requested or not actual:
        return False

    # Mandatory project check. Avoid matching records with no project name.
    return (
        requested == actual
        or requested in actual
        or actual in requested
    )


def area_points(difference):
    if difference <= 1:
        return 70
    if difference <= 5:
        return 65
    if difference <= 10:
        return 55
    return 0


def find_best_match(db_path, columns, project, bedrooms, listing_area):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row

    # Project is mandatory: query only records whose English project name
    # contains the requested project name.
    sql = """
        SELECT *
        FROM units
        WHERE lower(trim(project_name_en)) LIKE ?
    """
    rows = connection.execute(
        sql, (f"%{clean_text(project)}%",)
    ).fetchall()
    connection.close()

    candidates = []
    for row in rows:
        record = dict(row)
        dld_project = record.get("project_name_en", "")

        # Never accept a candidate without a matching project.
        if not project_matches(project, dld_project):
            continue

        candidate_area = dld_area_sqft(record, columns)
        if candidate_area is None or listing_area is None:
            continue

        difference = abs(candidate_area - listing_area)

        # A candidate more than 10 sqft away is not considered reliable.
        if difference > AREA_LIMIT_SQFT:
            continue

        score = area_points(difference)

        dld_bedrooms = number(record.get("rooms"))
        if bedrooms is not None and dld_bedrooms is not None:
            if int(dld_bedrooms) == bedrooms:
                score += 10

        # Small secondary bonus only; area remains the dominant signal.
        dld_type = clean_text(record.get("property_sub_type_en", ""))
        if "apartment" in dld_type or "flat" in dld_type:
            score += 5

        record["_area_sqft"] = candidate_area
        record["_area_difference"] = difference
        record["_score"] = score
        candidates.append(record)

    if not candidates:
        return None, 0

    # Area difference decides first; score breaks ties.
    candidates.sort(
        key=lambda item: (
            item["_area_difference"],
            -item["_score"]
        )
    )
    return candidates[0], len(candidates)


url = st.text_input(
    "Property Finder listing URL",
    placeholder="https://www.propertyfinder.ae/en/plp/..."
)

if st.button("Find Unit", type="primary"):
    if not url.strip():
        st.error("Please enter a Property Finder listing URL.")
        st.stop()

    parsed = urlparse(url.strip())
    if "propertyfinder.ae" not in parsed.netloc.lower():
        st.error("Please enter a valid Property Finder URL.")
        st.stop()

    try:
        with st.spinner("Reading listing details..."):
            listing = scrape_listing(url.strip())

        st.subheader("Listing details")
        st.write(f"**Title:** {listing['title'] or 'Not detected'}")

        # Allow corrections when the listing website blocks automatic parsing.
        project = st.text_input(
            "Project name (mandatory)",
            value=listing["project"]
        )

        area_default = (
            str(round(listing["area_sqft"], 2))
            if listing["area_sqft"] is not None
            else ""
        )
        area_input = st.text_input(
            "Listing area in sqft",
            value=area_default
        )

        bedrooms_default = (
            str(listing["bedrooms"])
            if listing["bedrooms"] is not None
            else ""
        )
        bedrooms_input = st.text_input(
            "Bedrooms",
            value=bedrooms_default
        )

        if st.button("Run Matching"):
            area_sqft = number(area_input)
            bedroom_count = (
                int(number(bedrooms_input))
                if number(bedrooms_input) is not None
                else None
            )

            if not project.strip():
                st.error("Project name is mandatory. Enter the project name.")
                st.stop()

            if area_sqft is None:
                st.error("Listing area could not be detected. Enter area in sqft.")
                st.stop()

            with st.spinner("Comparing DLD units..."):
                db_path, db_columns = load_database()
                best, eligible_count = find_best_match(
                    db_path,
                    db_columns,
                    project.strip(),
                    bedroom_count,
                    area_sqft
                )

            if best is None:
                st.error(
                    "No reliable match found. No record matched the mandatory "
                    "project name and was within 10 sqft of the listing area."
                )
                st.caption(
                    "Check the project name and listing area, and confirm that "
                    "the DLD database contains the relevant unit."
                )
                st.stop()

            st.success("Closest matching DLD record")
            st.metric("DLD Unit Number", best.get("unit_number", "Not available"))
            st.write(f"**Project:** {best.get('project_name_en', '')}")
            st.write(f"**Listing area:** {area_sqft:,.2f} sqft")
            st.write(f"**DLD area:** {best['_area_sqft']:,.2f} sqft")
            st.write(
                f"**Area difference:** {best['_area_difference']:.2f} sqft"
            )
            st.write(f"**Bedrooms:** {best.get('rooms', 'Not available')}")
            st.write(f"**Matching candidates within 10 sqft:** {eligible_count}")

            with st.expander("DLD record details"):
                st.json({
                    key: value
                    for key, value in best.items()
                    if not key.startswith("_")
                })

    except requests.HTTPError as error:
        st.error(f"Could not open the listing or download the database: {error}")
    except Exception as error:
        st.error(f"Error: {error}")
