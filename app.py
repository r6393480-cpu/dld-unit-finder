
import gzip
import os
import re
import sqlite3
import tempfile
from urllib.parse import urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

st.set_page_config(
    page_title="Dubai DLD Unit Finder",
    page_icon="🏢",
    layout="centered",
)

SQFT_PER_SQM = 10.7639104167
MAX_AREA_DIFFERENCE = 10.0
DATABASE_RELEASE = "latest-data"
DATABASE_FILENAME = "dld_units.db.gz"


# --------------------------------------------------
# TEXT AND NUMBER HELPERS
# --------------------------------------------------

def clean_text(value):
    value = str(value or "").lower().strip()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def parse_number(value):
    if value is None:
        return None

    try:
        value = str(value).replace(",", "").strip()
        result = float(value)
        return result if result > 0 else None
    except (ValueError, TypeError):
        return None


def extract_number(patterns, text):
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = parse_number(match.group(1))
            if value is not None:
                return value
    return None


def get_github_repo():
    try:
        repo = st.secrets.get("GITHUB_REPO", "")
    except Exception:
        repo = ""

    return repo or os.getenv(
        "GITHUB_REPO",
        "r6393480-cpu/dld-unit-finder",
    )


# --------------------------------------------------
# DATABASE
# --------------------------------------------------

@st.cache_resource(show_spinner=False)
def load_database():
    repo = get_github_repo()

    database_url = (
        f"https://github.com/{repo}/releases/download/"
        f"{DATABASE_RELEASE}/{DATABASE_FILENAME}"
    )

    response = requests.get(
        database_url,
        timeout=180,
        headers={"User-Agent": "Dubai-DLD-Unit-Finder"},
    )
    response.raise_for_status()

    try:
        database_bytes = gzip.decompress(response.content)
    except OSError as exc:
        raise RuntimeError(
            "Database download hui, lekin gzip file open nahi hui. "
            "GitHub release mein dld_units.db.gz check karo."
        ) from exc

    database_path = os.path.join(
        tempfile.gettempdir(),
        "dld_units.db",
    )

    with open(database_path, "wb") as file:
        file.write(database_bytes)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row

    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()

    table_names = [row["name"] for row in tables]

    if "units" not in table_names:
        connection.close()
        raise RuntimeError(
            f"Database mein 'units' table nahi mili. "
            f"Available tables: {table_names}"
        )

    columns = [
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(units)"
        ).fetchall()
    ]

    if "project_name_en" not in columns:
        connection.close()
        raise RuntimeError(
            "Database ke units table mein project_name_en column nahi hai."
        )

    connection.close()

    return database_path, columns


# --------------------------------------------------
# PROPERTY FINDER SCRAPER
# --------------------------------------------------

def scrape_listing(url):
    response = requests.get(
        url,
        timeout=30,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        },
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    title_tag = soup.find("title")
    title = (
        title_tag.get_text(" ", strip=True)
        if title_tag
        else ""
    )

    page_text = soup.get_text(" ", strip=True)

    project = ""

    # Example: Sale in Serenova: ...
    match = re.search(
        r"(?:sale|rent)\s+in\s+([^:|]+)",
        title,
        re.IGNORECASE,
    )

    if match:
        project = match.group(1).strip()

    # Try Open Graph title if needed.
    if not project:
        meta = soup.find("meta", property="og:title")

        if meta and meta.get("content"):
            match = re.search(
                r"(?:sale|rent)\s+in\s+([^:|]+)",
                meta["content"],
                re.IGNORECASE,
            )

            if match:
                project = match.group(1).strip()

    # Bedrooms
    bedrooms = extract_number(
        [
            r"\b(\d+)\s*bedrooms?\b",
            r"\b(\d+)\s*beds?\b",
            r"\b(\d+)\s*b/?r\b",
        ],
        page_text,
    )

    # Area: identify units explicitly.
    area_sqft = None

    sqft_value = extract_number(
        [
            r"([\d,]+(?:\.\d+)?)\s*sq\.?\s*ft\b",
            r"([\d,]+(?:\.\d+)?)\s*sqft\b",
            r"([\d,]+(?:\.\d+)?)\s*ft²\b",
            r"([\d,]+(?:\.\d+)?)\s*square feet\b",
        ],
        page_text,
    )

    sqm_value = extract_number(
        [
            r"([\d,]+(?:\.\d+)?)\s*sq\.?\s*m\b",
            r"([\d,]+(?:\.\d+)?)\s*sqm\b",
            r"([\d,]+(?:\.\d+)?)\s*m²\b",
            r"([\d,]+(?:\.\d+)?)\s*square metres?\b",
        ],
        page_text,
    )

    if sqft_value is not None:
        area_sqft = sqft_value
    elif sqm_value is not None:
        area_sqft = sqm_value * SQFT_PER_SQM

    return {
        "title": title,
        "project": project,
        "bedrooms": (
            int(bedrooms)
            if bedrooms is not None
            else None
        ),
        "area_sqft": area_sqft,
    }


# --------------------------------------------------
# DLD AREA NORMALIZATION
# --------------------------------------------------

def get_dld_area_sqft(record, columns):
    """
    Prefer explicitly named sqft fields.
    Otherwise actual_area is treated as square metres,
    based on the provided example: 130 m² ≈ 1,399.31 sqft.
    """

    sqft_columns = [
        "area_sqft",
        "unit_area_sqft",
        "actual_area_sqft",
    ]

    for column in sqft_columns:
        if column in columns:
            value = parse_number(record.get(column))

            if value is not None:
                return value

    if "actual_area" in columns:
        value = parse_number(record.get("actual_area"))

        if value is not None:
            return value * SQFT_PER_SQM

    return None


# --------------------------------------------------
# MANDATORY PROJECT MATCHING
# --------------------------------------------------

def project_matches(requested_project, dld_project):
    requested = clean_text(requested_project)
    actual = clean_text(dld_project)

    if not requested or not actual:
        return False

    # Require a full normalized project-name match.
    # This avoids accepting unrelated projects just because
    # they share one common word.
    return requested == actual


# --------------------------------------------------
# AREA SCORING
# --------------------------------------------------

def calculate_area_score(difference):
    if difference <= 1:
        return 70

    if difference <= 2:
        return 68

    if difference <= 5:
        return 62

    if difference <= 10:
        return 50

    return 0


def find_best_matches(
    database_path,
    columns,
    project,
    bedrooms,
    listing_area_sqft,
):
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row

    # Search within the requested project.
    # Final project validation is performed separately.
    rows = connection.execute(
        """
        SELECT *
        FROM units
        WHERE lower(trim(project_name_en)) LIKE ?
        """,
        (f"%{clean_text(project)}%",),
    ).fetchall()

    connection.close()

    candidates = []

    for sqlite_row in rows:
        record = dict(sqlite_row)

        dld_project = record.get("project_name_en", "")

        # Project name is mandatory.
        if not project_matches(project, dld_project):
            continue

        dld_area_sqft = get_dld_area_sqft(record, columns)

        if dld_area_sqft is None:
            continue

        difference = abs(dld_area_sqft - listing_area_sqft)

        # Reject candidates that are too far from the listing area.
        if difference > MAX_AREA_DIFFERENCE:
            continue

        score = calculate_area_score(difference)

        # Bedrooms are secondary to area.
        dld_bedrooms = parse_number(record.get("rooms"))

        if bedrooms is not None and dld_bedrooms is not None:
            if int(dld_bedrooms) == bedrooms:
                score += 10

        # Small secondary bonus for apartment/flat records.
        property_subtype = clean_text(
            record.get("property_sub_type_en", "")
        )

        if (
            "apartment" in property_subtype
            or "flat" in property_subtype
        ):
            score += 5

        record["_area_sqft"] = dld_area_sqft
        record["_area_difference"] = difference
        record["_score"] = score

        candidates.append(record)

    # Closest area first; secondary score breaks ties.
    candidates.sort(
        key=lambda record: (
            record["_area_difference"],
            -record["_score"],
        )
    )

    return candidates


# --------------------------------------------------
# STREAMLIT UI
# --------------------------------------------------

st.title("Dubai DLD Unit Finder")

st.write(
    "Enter a Property Finder listing URL, verify the project "
    "and area, then search for the closest DLD unit."
)

with st.form("listing_form"):
    listing_url = st.text_input(
        "Property Finder listing URL",
        placeholder=(
            "https://www.propertyfinder.ae/en/plp/..."
        ),
    )

    fetch_listing = st.form_submit_button(
        "Read Listing Details",
        type="secondary",
    )


if fetch_listing:
    if not listing_url.strip():
        st.error("Please enter a Property Finder URL.")
        st.stop()

    parsed_url = urlparse(listing_url.strip())

    if (
        parsed_url.scheme not in ("http", "https")
        or "propertyfinder.ae" not in parsed_url.netloc.lower()
    ):
        st.error("Please enter a valid Property Finder URL.")
        st.stop()

    try:
        with st.spinner("Reading listing details..."):
            listing = scrape_listing(listing_url.strip())

        st.session_state["listing_details"] = listing
        st.session_state["listing_url"] = listing_url.strip()

    except Exception as error:
        st.error(f"Could not read listing: {error}")


# Keep the details visible after Streamlit reruns.
listing = st.session_state.get("listing_details")

if listing:
    st.subheader("Listing Details")

    st.write(f"**Title:** {listing['title'] or 'Not detected'}")

    with st.form("matching_form"):
        project_name = st.text_input(
            "Project name (mandatory)",
            value=listing.get("project") or "",
        )

        default_area = listing.get("area_sqft")
        area_value = (
            str(round(default_area, 2))
            if default_area is not None
            else ""
        )

        area_input = st.text_input(
            "Listing area in sqft",
            value=area_value,
        )

        default_bedrooms = listing.get("bedrooms")
        bedrooms_value = (
            str(default_bedrooms)
            if default_bedrooms is not None
            else ""
        )

        bedrooms_input = st.text_input(
            "Bedrooms",
            value=bedrooms_value,
        )

        run_matching = st.form_submit_button(
            "Run Matching",
            type="primary",
        )

    # IMPORTANT:
    # This block is outside the form and runs after submission.
    if run_matching:
        if not project_name.strip():
            st.error("Project name is mandatory.")
            st.stop()

        listing_area_sqft = parse_number(area_input)

        if listing_area_sqft is None:
            st.error("Enter the listing area in sqft.")
            st.stop()

        bedrooms_number = parse_number(bedrooms_input)

        if bedrooms_number is not None:
            bedrooms_number = int(bedrooms_number)

        try:
            with st.spinner("Loading DLD database..."):
                database_path, database_columns = load_database()

            with st.spinner("Searching for matching units..."):
                matches = find_best_matches(
                    database_path=database_path,
                    columns=database_columns,
                    project=project_name.strip(),
                    bedrooms=bedrooms_number,
                    listing_area_sqft=listing_area_sqft,
                )

            if not matches:
                st.error(
                    "No matching unit found within 10 sqft. "
                    "Check the project name, area and DLD database."
                )
                st.info(
                    "The app will not select a unit with a large area "
                    "mismatch just because its project or bedrooms match."
                )
                st.stop()

            best = matches[0]

            st.success("Closest matching DLD record")

            st.metric(
                "DLD Unit Number",
                str(best.get("unit_number") or "Not available"),
            )

            st.write(
                f"**Project:** {best.get('project_name_en', '')}"
            )
            st.write(
                f"**Listing area:** {listing_area_sqft:,.2f} sqft"
            )
            st.write(
                f"**DLD area:** {best['_area_sqft']:,.2f} sqft"
            )
            st.write(
                f"**Area difference:** "
                f"{best['_area_difference']:.2f} sqft"
            )
            st.write(f"**Bedrooms:** {best.get('rooms', 'Unknown')}")
            st.write(f"**Candidates within 10 sqft:** {len(matches)}")

            if best["_area_difference"] > 1:
                st.warning(
                    "This is the closest candidate, not an exact-area match."
                )

            if len(matches) > 1:
                with st.expander("View other matching candidates"):
                    for index, candidate in enumerate(matches[:10], start=1):
                        st.markdown(
                            f"**{index}. Unit "
                            f"{candidate.get('unit_number', 'Unknown')}** — "
                            f"{candidate['_area_sqft']:,.2f} sqft; "
                            f"difference "
                            f"{candidate['_area_difference']:.2f} sqft; "
                            f"score {candidate['_score']}"
                        )

            with st.expander("DLD record details"):
                st.json({
                    key: value
                    for key, value in best.items()
                    if not key.startswith("_")
                })

        except Exception as error:
            st.error(f"Matching failed: {error}")
