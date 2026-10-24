import os
import re
import time
import gzip
import shutil
import sqlite3
from difflib import SequenceMatcher
from urllib.parse import urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup


# =========================================================
# CONFIGURATION
# =========================================================

st.set_page_config(
    page_title="DLD Unit Finder",
    page_icon="🏠",
    layout="centered",
)

DB_PATH = "dld_units.db"
DB_GZ_PATH = "dld_units.db.gz"
RELEASE_TAG = "latest-data"

GITHUB_REPO = os.environ.get(
    "GITHUB_REPO",
    st.secrets.get("GITHUB_REPO", "r6393480-cpu/dld-unit-finder")
)

GITHUB_TOKEN = os.environ.get(
    "GITHUB_TOKEN",
    st.secrets.get("GITHUB_TOKEN", "")
)


# =========================================================
# STYLING
# =========================================================

st.markdown("""
<style>
.main-title {
    text-align: center;
    font-size: 32px;
    font-weight: 800;
    margin-bottom: 5px;
}
.subtitle {
    text-align: center;
    color: #777;
    margin-bottom: 25px;
}
.result-card {
    border: 1px solid #dddddd;
    border-radius: 12px;
    padding: 18px;
    margin-top: 15px;
}
.unit-number {
    font-size: 38px;
    font-weight: 800;
}
.small-label {
    color: #777;
    font-size: 13px;
}
</style>
""", unsafe_allow_html=True)

st.markdown(
    '<div class="main-title">🏠 DLD Unit Finder</div>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="subtitle">Property Finder URL → DLD unit details</div>',
    unsafe_allow_html=True,
)


# =========================================================
# DATABASE DOWNLOAD
# =========================================================

def download_database(force=False):
    """Download and validate SQLite database from GitHub Releases."""

    if (
        not force
        and os.path.exists(DB_PATH)
        and os.path.getsize(DB_PATH) > 0
    ):
        age_hours = (time.time() - os.path.getmtime(DB_PATH)) / 3600

        if age_hours < 24:
            return True

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "DLD-Unit-Finder",
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    api_url = (
        f"https://api.github.com/repos/{GITHUB_REPO}"
        f"/releases/tags/{RELEASE_TAG}"
    )

    try:
        response = requests.get(
            api_url,
            headers=headers,
            timeout=30,
        )

        if response.status_code != 200:
            st.error(
                f"GitHub Release access failed "
                f"(HTTP {response.status_code}). "
                f"Check GITHUB_REPO and the latest-data release."
            )
            return False

        release = response.json()

        asset_url = None

        for asset in release.get("assets", []):
            if asset.get("name") == "dld_units.db.gz":
                asset_url = asset.get("browser_download_url")
                break

        if not asset_url:
            st.error(
                "Release mein dld_units.db.gz file nahi mili."
            )
            return False

        with st.spinner("Downloading DLD database..."):
            response = requests.get(
                asset_url,
                headers=headers,
                stream=True,
                timeout=(30, 600),
            )
            response.raise_for_status()

            with open(DB_GZ_PATH, "wb") as file:
                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):
                    if chunk:
                        file.write(chunk)

            with gzip.open(DB_GZ_PATH, "rb") as source:
                with open(DB_PATH, "wb") as destination:
                    shutil.copyfileobj(source, destination)

        # Validate SQLite database
        connection = sqlite3.connect(DB_PATH)

        try:
            tables = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table'"
            ).fetchall()

            if not any(row[0] == "units" for row in tables):
                raise ValueError(
                    "Database mein required 'units' table nahi hai."
                )

            connection.execute("PRAGMA schema_version").fetchone()

        finally:
            connection.close()

        if os.path.exists(DB_GZ_PATH):
            os.remove(DB_GZ_PATH)

        return True

    except Exception as error:
        for path in (DB_GZ_PATH,):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

        st.error(f"Database download failed: {error}")
        return False


@st.cache_resource
def get_db():
    """Open the SQLite database."""

    if not os.path.exists(DB_PATH):
        return None

    connection = sqlite3.connect(
        DB_PATH,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row

    return connection


def get_columns(connection):
    return {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(units)"
        ).fetchall()
    }


# =========================================================
# PROPERTY FINDER URL PARSER
# =========================================================

def parse_pf_url(url):
    """
    Extract project candidates and location from a Property Finder URL.

    Example:
    apartment-for-sale-dubai-dubai-silicon-oasis-serenova-163243433.html

    Project candidate: serenova
    Location context: silicon oasis
    """

    data = {
        "source_url": url,
        "project_candidates": [],
        "location": "",
        "property_type": "",
        "listing_id": "",
        "title": "",
        "bedrooms": None,
        "area_sqft": None,
    }

    parsed = urlparse(url)

    slug = parsed.path.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.html?$", "", slug, flags=re.IGNORECASE)

    parts = slug.lower().split("-")

    # Remove the listing ID at the end.
    if parts and parts[-1].isdigit():
        data["listing_id"] = parts[-1]
        parts = parts[:-1]

    property_types = {
        "apartment": "Apartment",
        "villa": "Villa",
        "townhouse": "Townhouse",
        "penthouse": "Penthouse",
        "duplex": "Duplex",
        "studio": "Studio",
    }

    for part in parts:
        if part in property_types:
            data["property_type"] = property_types[part]
            break

    # Find the location after the sale/rent segment.
    marker_index = -1

    for i in range(len(parts) - 1):
        if parts[i] == "for" and parts[i + 1] in (
            "sale", "rent", "buy", "lease"
        ):
            marker_index = i + 2
            break

    location_parts = []

    if marker_index >= 0:
        remaining = parts[marker_index:]

        if "dubai" in remaining:
            dubai_index = remaining.index("dubai")
            location_parts = remaining[dubai_index + 1:]

            # Remove repeated "dubai" in URL slugs.
            location_parts = [
                word for word in location_parts
                if word != "dubai"
            ]

    # Keep only meaningful URL location information.
    data["location"] = " ".join(location_parts)

    # Candidate projects are generated from the END of the slug.
    # This prioritizes names like "serenova" over "silicon oasis".
    stop_words = {
        "for", "sale", "rent", "buy", "lease",
        "apartment", "villa", "townhouse", "penthouse",
        "duplex", "studio", "bedroom", "bedrooms",
    }

    clean = [
        word for word in location_parts
        if word not in stop_words
    ]

    candidates = []

    for count in range(1, min(5, len(clean) + 1)):
        project = " ".join(clean[-count:])
        context = " ".join(clean[:-count])

        if project:
            candidates.append({
                "project": project,
                "context": context,
            })

    data["project_candidates"] = candidates

    return data


# =========================================================
# PROPERTY PAGE SCRAPER
# =========================================================

def scrape_pf_page(url):
    """Attempt to retrieve listing title, bedrooms and area."""

    result = {
        "title": "",
        "bedrooms": None,
        "area_sqft": None,
    }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/132.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=15,
        )

        if response.status_code != 200:
            return result

        soup = BeautifulSoup(response.text, "html.parser")

        title_tag = soup.find("meta", property="og:title")

        if title_tag:
            result["title"] = title_tag.get("content", "")

        if not result["title"] and soup.title:
            result["title"] = soup.title.get_text(" ", strip=True)

        text = soup.get_text(" ", strip=True)

        bedroom_match = re.search(
            r"\b(\d{1,2})\s*(?:bedrooms?|beds|br)\b",
            text,
            re.IGNORECASE,
        )

        if bedroom_match:
            result["bedrooms"] = int(bedroom_match.group(1))

        area_match = re.search(
            r"([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|square feet)\b",
            text,
            re.IGNORECASE,
        )

        if area_match:
            result["area_sqft"] = float(
                area_match.group(1).replace(",", "")
            )

    except Exception:
        # Property Finder may block automated requests.
        pass

    return result


# =========================================================
# NORMALIZATION AND SCORING
# =========================================================

def normalize(value):
    if value is None:
        return ""

    value = str(value).lower()
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()

    return value


def similarity(a, b):
    a = normalize(a)
    b = normalize(b)

    if not a or not b:
        return 0.0

    if a == b:
        return 1.0

    return SequenceMatcher(None, a, b).ratio()


def get_value(row, possible_names):
    """Read a field even if the database uses slightly different names."""

    available = {key.lower(): key for key in row.keys()}

    for name in possible_names:
        actual_name = available.get(name.lower())

        if actual_name:
            return row[actual_name]

    return None


def score_row(row, prop):
    """
    Score a DLD record using project, location and available listing details.

    The score is a ranking score, not a statistically calibrated probability.
    """

    project_name = normalize(
        get_value(row, ["project_name_en", "project_name"])
    )

    master_project = normalize(
        get_value(row, ["master_project_en", "master_project"])
    )

    area_name = normalize(
        get_value(row, ["area_name_en", "area_name"])
    )

    row_type = normalize(
        get_value(row, ["property_type_en", "property_type", "property_type_name"])
    )

    row_bedrooms = get_value(
        row,
        ["rooms", "bedrooms", "room_count"],
    )

    row_area = get_value(
        row,
        ["actual_area", "area_sqft", "unit_area"],
    )

    best_project_score = 0.0
    best_context_score = 0.0

    for candidate in prop.get("project_candidates", []):
        candidate_name = normalize(candidate["project"])
        context = normalize(candidate["context"])

        # Project match is the primary signal.
        if candidate_name:
            exact = candidate_name == project_name

            contains = (
                candidate_name in project_name
                or project_name in candidate_name
            )

            if exact:
                current_score = 100.0
            elif contains:
                current_score = 85.0
            else:
                current_score = (
                    similarity(candidate_name, project_name) * 60
                )

            # A single generic word is not enough for high confidence.
            if len(candidate_name.split()) == 1:
                current_score = min(current_score, 65.0)

            best_project_score = max(
                best_project_score,
                current_score,
            )

        # Location context helps separate projects in different areas.
        if context:
            context_matches = (
                context in area_name
                or context in master_project
                or area_name in context
            )

            if context_matches:
                best_context_score = max(
                    best_context_score,
                    20.0,
                )
            else:
                context_words = [
                    word for word in context.split()
                    if len(word) > 2
                ]

                matching_words = sum(
                    1 for word in context_words
                    if word in area_name or word in master_project
                )

                if context_words:
                    best_context_score = max(
                        best_context_score,
                        15.0 * matching_words / len(context_words),
                    )

    score = best_project_score + best_context_score

    # Additional details are useful only if the page was successfully scraped.
    prop_type = normalize(prop.get("property_type"))

    if prop_type and row_type:
        if prop_type in row_type or row_type in prop_type:
            score += 8
        else:
            score -= 8

    if prop.get("bedrooms") is not None and row_bedrooms is not None:
        try:
            if int(float(row_bedrooms)) == int(prop["bedrooms"]):
                score += 10
            else:
                score -= 10
        except (ValueError, TypeError):
            pass

    if prop.get("area_sqft") is not None and row_area is not None:
        try:
            listing_area = float(prop["area_sqft"])
            database_area = float(
                str(row_area).replace(",", "")
            )

            if listing_area > 0 and database_area > 0:
                difference = (
                    abs(listing_area - database_area) / listing_area
                )

                if difference <= 0.05:
                    score += 15
                elif difference <= 0.15:
                    score += 10
                elif difference <= 0.30:
                    score += 3
                else:
                    score -= 5

        except (ValueError, TypeError):
            pass

    return round(score, 2)


# =========================================================
# SEARCH DATABASE
# =========================================================

def find_units(connection, prop):
    """
    Search for likely records and return only one strong result.

    Important: if the data does not support a reliable match, return no result
    rather than selecting a random unit.
    """

    columns = get_columns(connection)

    required_columns = {
        "project_name_en",
        "unit_number",
    }

    if not required_columns.issubset(columns):
        st.error(
            "Database schema expected columns nahi rakhta. "
            "Check karein ke correct DLD database download hui hai."
        )
        return None

    candidates = prop.get("project_candidates", [])

    if not candidates:
        return None

    rows_by_key = {}

    # Search the most specific project candidates first.
    for candidate in candidates:
        project = normalize(candidate["project"])

        if len(project) < 3:
            continue

        # Avoid generic one-word matches.
        generic_words = {
            "residence", "residences", "tower", "towers",
            "building", "garden", "gardens", "park",
            "view", "views", "city", "bay", "heights",
        }

        if len(project.split()) == 1 and project in generic_words:
            continue

        escaped = (
            project.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )

        query = """
            SELECT *
            FROM units
            WHERE LOWER(COALESCE(project_name_en, '')) LIKE ? ESCAPE '\\'
               OR LOWER(COALESCE(master_project_en, '')) LIKE ? ESCAPE '\\'
            LIMIT 1500
        """

        try:
            rows = connection.execute(
                query,
                (f"%{escaped}%", f"%{escaped}%"),
            ).fetchall()
        except sqlite3.Error as error:
            st.error(f"Database search error: {error}")
            return None

        for row in rows:
            key = (
                str(get_value(row, ["unit_number"]) or ""),
                str(get_value(row, ["land_number"]) or ""),
                str(get_value(row, ["project_name_en"]) or ""),
                str(get_value(row, ["master_project_en"]) or ""),
            )

            rows_by_key[key] = row

        # Do not stop after the first weak candidate.
        # Broader candidates can produce useful records too.

    if not rows_by_key:
        return None

    ranked = []

    for row in rows_by_key.values():
        score = score_row(row, prop)
        ranked.append((score, row))

    ranked.sort(key=lambda item: item[0], reverse=True)

    best_score, best_row = ranked[0]

    # Prevent weak results from being presented as accurate matches.
    # The threshold is a conservative ranking rule, not a probability.
    if best_score < 75:
        return None

    # If the top records have nearly identical scores but different units,
    # the app cannot safely claim the first one is uniquely correct.
    if len(ranked) > 1:
        second_score, second_row = ranked[1]

        first_unit = str(
            get_value(best_row, ["unit_number"]) or ""
        )

        second_unit = str(
            get_value(second_row, ["unit_number"]) or ""
        )

        if (
            first_unit != second_unit
            and best_score - second_score < 5
        ):
            return {
                "ambiguous": True,
                "best_score": best_score,
                "second_score": second_score,
            }

    return {
        "ambiguous": False,
        "score": best_score,
        "row": best_row,
    }


# =========================================================
# DISPLAY RESULT
# =========================================================

def show_result(result):
    if result is None:
        st.warning(
            "Reliable match nahi mila. Project DLD database mein "
            "registered na ho, ya listing details insufficient ho sakti hain."
        )
        return

    if result.get("ambiguous"):
        st.warning(
            "Is listing ke liye multiple similar records mil rahe hain. "
            "Sirf ek unit ko accurate kehna reliable nahi hoga. "
            "Listing ka exact area aur bedroom count verify karein."
        )
        return

    row = result["row"]
    score = result["score"]

    unit_number = get_value(row, ["unit_number"])
    project_name = get_value(row, ["project_name_en"])
    master_project = get_value(row, ["master_project_en"])
    area_name = get_value(row, ["area_name_en"])
    land_number = get_value(row, ["land_number"])

    st.success("Best matching DLD record")

    st.markdown(
        f'<div class="result-card">'
        f'<div class="small-label">UNIT NUMBER</div>'
        f'<div class="unit-number">{unit_number or "N/A"}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    st.write(f"**Project:** {project_name or 'N/A'}")
    st.write(f"**Master Project:** {master_project or 'N/A'}")
    st.write(f"**Area:** {area_name or 'N/A'}")
    st.write(f"**Land Number:** {land_number or 'N/A'}")

    st.caption(
        f"Internal match score: {score}. "
        "Yeh probability percentage nahi hai."
    )

    with st.expander("View all DLD fields"):
        st.json(dict(row))


# =========================================================
# SIDEBAR
# =========================================================

with st.sidebar:
    st.header("⚙️ Settings")

    st.caption(f"Database repository: {GITHUB_REPO}")

    if st.button("🔄 Update Database", use_container_width=True):
        if download_database(force=True):
            get_db.clear()
            st.success("Database updated successfully.")
            st.rerun()

    st.divider()

    st.caption(
        "Data source: DLD open data. "
        "Some off-plan properties may not be registered."
    )


# =========================================================
# MAIN SEARCH
# =========================================================

url = st.text_input(
    "Property Finder listing URL",
    placeholder="https://www.propertyfinder.ae/en/plp/...",
)

search_button = st.button(
    "🔍 Find Unit",
    type="primary",
    use_container_width=True,
)

if search_button:
    if not url.strip():
        st.warning("Pehle Property Finder URL enter karein.")

    elif "propertyfinder.ae" not in urlparse(url).netloc.lower():
        st.warning("Please enter a valid Property Finder URL.")

    else:
        if download_database():
            connection = get_db()

            if connection is None:
                st.error("Database open nahi ho saki.")
            else:
                with st.spinner("Listing details aur DLD records check ho rahe hain..."):
                    prop = parse_pf_url(url)

                    scraped = scrape_pf_page(url)
                    prop.update(scraped)

                    # Search only after extracting the available details.
                    result = find_units(connection, prop)

                st.divider()

                with st.expander("Extracted listing details"):
                    st.write(
                        "**Project candidates:**",
                        [
                            candidate["project"]
                            for candidate in prop["project_candidates"]
                        ],
                    )
                    st.write("**URL location:**", prop.get("location"))
                    st.write("**Property type:**", prop.get("property_type"))
                    st.write("**Listing title:**", prop.get("title") or "Not available")
                    st.write("**Bedrooms:**", prop.get("bedrooms") or "Not available")
                    st.write("**Area (sq ft):**", prop.get("area_sqft") or "Not available")

                show_result(result)
