import os
import re
import gzip
import shutil
import sqlite3
import time
from difflib import SequenceMatcher
from urllib.parse import urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup


# =====================================================
# CONFIG
# =====================================================

st.set_page_config(
    page_title="DLD Unit Finder",
    page_icon="🏠",
    layout="centered",
)

DB_PATH = "dld_units.db"
GZ_PATH = "dld_units.db.gz"
RELEASE_TAG = "latest-data"

GITHUB_REPO = os.environ.get("GITHUB_REPO", "")

if not GITHUB_REPO:
    try:
        GITHUB_REPO = st.secrets.get(
            "GITHUB_REPO",
            "r6393480-cpu/dld-unit-finder",
        )
    except Exception:
        GITHUB_REPO = "r6393480-cpu/dld-unit-finder"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

if not GITHUB_TOKEN:
    try:
        GITHUB_TOKEN = st.secrets.get("GITHUB_TOKEN", "")
    except Exception:
        GITHUB_TOKEN = ""


st.title("🏠 DLD Unit Finder")
st.caption("Property Finder listing → DLD unit record")


# =====================================================
# DATABASE DOWNLOAD
# =====================================================

def download_database(force=False):
    if os.path.exists(DB_PATH) and not force:
        age = time.time() - os.path.getmtime(DB_PATH)

        if age < 24 * 60 * 60:
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
        response.raise_for_status()

        release = response.json()
        download_url = None

        for asset in release.get("assets", []):
            if asset["name"] == "dld_units.db.gz":
                download_url = asset["browser_download_url"]
                break

        if not download_url:
            st.error(
                "Release mein dld_units.db.gz file nahi mili."
            )
            return False

        with st.spinner("Downloading DLD database..."):
            response = requests.get(
                download_url,
                headers=headers,
                stream=True,
                timeout=(30, 600),
            )
            response.raise_for_status()

            with open(GZ_PATH, "wb") as file:
                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):
                    if chunk:
                        file.write(chunk)

            with gzip.open(GZ_PATH, "rb") as source:
                with open(DB_PATH, "wb") as destination:
                    shutil.copyfileobj(source, destination)

        connection = sqlite3.connect(DB_PATH)

        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        connection.close()

        if "units" not in tables:
            raise ValueError(
                "Database mein 'units' table nahi mili."
            )

        if os.path.exists(GZ_PATH):
            os.remove(GZ_PATH)

        return True

    except Exception as error:
        st.error(f"Database download failed: {error}")
        return False


@st.cache_resource
def get_db():
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


# =====================================================
# TEXT UTILITIES
# =====================================================

def normalize(value):
    if value is None:
        return ""

    value = str(value).lower()
    value = re.sub(r"[^a-z0-9\s.]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def get_value(row, names):
    available = {key.lower(): key for key in row.keys()}

    for name in names:
        actual = available.get(name.lower())

        if actual is not None:
            return row[actual]

    return None


def similarity(a, b):
    a = normalize(a)
    b = normalize(b)

    if not a or not b:
        return 0.0

    if a == b:
        return 1.0

    return SequenceMatcher(None, a, b).ratio()


def parse_number(value):
    if value is None:
        return None

    match = re.search(
        r"\d+(?:,\d{3})*(?:\.\d+)?",
        str(value),
    )

    if not match:
        return None

    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_bedrooms(value):
    if value is None:
        return None

    text = normalize(value)

    if "studio" in text:
        return 0

    number = parse_number(text)

    if number is None:
        return None

    return int(number)


# =====================================================
# PROPERTY FINDER URL
# =====================================================

def parse_pf_url(url):
    result = {
        "source_url": url,
        "project_candidates": [],
        "location": "",
        "property_type": "",
        "title": "",
        "bedrooms": None,
        "area_sqft": None,
    }

    parsed = urlparse(url)
    slug = parsed.path.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.html?$", "", slug, flags=re.I)

    parts = slug.lower().split("-")

    if parts and parts[-1].isdigit():
        parts = parts[:-1]

    types = {
        "apartment": "Apartment",
        "villa": "Villa",
        "townhouse": "Townhouse",
        "penthouse": "Penthouse",
        "duplex": "Duplex",
        "studio": "Studio",
    }

    for word in parts:
        if word in types:
            result["property_type"] = types[word]
            break

    start = None

    for index in range(len(parts) - 1):
        if (
            parts[index] == "for"
            and parts[index + 1] in {"sale", "rent", "buy", "lease"}
        ):
            start = index + 2
            break

    if start is None:
        return result

    location_parts = parts[start:]

    if "dubai" in location_parts:
        location_parts = location_parts[
            location_parts.index("dubai") + 1:
        ]

    location_parts = [
        word for word in location_parts
        if word != "dubai"
    ]

    result["location"] = " ".join(location_parts)

    # Generate candidate names from the end of the URL slug.
    # For this listing, "serenova" is the most specific candidate.
    candidates = []

    for count in range(1, min(5, len(location_parts) + 1)):
        project = " ".join(location_parts[-count:])
        context = " ".join(location_parts[:-count])

        if project:
            candidates.append({
                "project": project,
                "context": context,
            })

    result["project_candidates"] = candidates

    return result


# =====================================================
# SCRAPE LISTING DETAILS
# =====================================================

def scrape_pf_page(url):
    result = {
        "title": "",
        "bedrooms": None,
        "area_sqft": None,
    }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/132.0 Safari/537.36"
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

        title = soup.find("meta", property="og:title")

        if title:
            result["title"] = title.get("content", "")

        if not result["title"] and soup.title:
            result["title"] = soup.title.get_text(" ", strip=True)

        text = soup.get_text(" ", strip=True)

        bedroom_match = re.search(
            r"\b(\d{1,2})\s*(?:bedrooms?|beds|br)\b",
            text,
            re.I,
        )

        if bedroom_match:
            result["bedrooms"] = int(bedroom_match.group(1))

        area_match = re.search(
            r"([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|square feet)\b",
            text,
            re.I,
        )

        if area_match:
            result["area_sqft"] = float(
                area_match.group(1).replace(",", "")
            )

    except Exception:
        pass

    return result


# =====================================================
# MATCH SCORING
# =====================================================

def score_record(row, prop):
    project_name = normalize(
        get_value(row, ["project_name_en", "project_name"])
    )

    master_project = normalize(
        get_value(row, ["master_project_en", "master_project"])
    )

    area_name = normalize(
        get_value(row, ["area_name_en", "area_name"])
    )

    score = 0.0
    best_project_score = 0.0
    best_context_score = 0.0

    for candidate in prop["project_candidates"]:
        project = normalize(candidate["project"])
        context = normalize(candidate["context"])

        if not project:
            continue

        # Project name is the most important signal.
        if project == project_name:
            project_score = 100
        elif project in project_name or project_name in project:
            project_score = 85
        else:
            project_score = similarity(
                project,
                project_name,
            ) * 60

        # Avoid treating a single generic word as an exact project.
        if len(project.split()) == 1 and project != project_name:
            project_score = min(project_score, 65)

        best_project_score = max(
            best_project_score,
            project_score,
        )

        # Location is supporting evidence, not the main project match.
        if context:
            context_words = [
                word for word in context.split()
                if len(word) > 2
            ]

            if context_words:
                matched = sum(
                    1 for word in context_words
                    if word in area_name or word in master_project
                )

                context_score = 15 * matched / len(context_words)

                best_context_score = max(
                    best_context_score,
                    context_score,
                )

    score += best_project_score
    score += best_context_score

    # Property type check.
    row_type = normalize(
        get_value(
            row,
            ["property_type_en", "property_type", "property_type_name"],
        )
    )

    requested_type = normalize(prop.get("property_type"))

    if row_type and requested_type:
        if requested_type in row_type or row_type in requested_type:
            score += 8
        else:
            score -= 8

    # Bedroom count check.
    row_bedrooms = parse_bedrooms(
        get_value(row, ["rooms", "bedrooms", "room_count"])
    )

    requested_bedrooms = prop.get("bedrooms")

    if (
        requested_bedrooms is not None
        and row_bedrooms is not None
    ):
        if row_bedrooms == int(requested_bedrooms):
            score += 20
        else:
            score -= 20

    # Area check: reward a close match to 1231 sqft.
    row_area = parse_number(
        get_value(
            row,
            ["actual_area", "area_sqft", "unit_area"],
        )
    )

    requested_area = prop.get("area_sqft")

    if (
        requested_area is not None
        and row_area is not None
        and requested_area > 0
        and row_area > 0
    ):
        difference = abs(row_area - requested_area) / requested_area

        if difference <= 0.02:
            score += 30
        elif difference <= 0.05:
            score += 25
        elif difference <= 0.10:
            score += 15
        elif difference <= 0.15:
            score += 5
        else:
            score -= 10

    return round(score, 2)


# =====================================================
# DATABASE SEARCH
# =====================================================

def find_best_match(connection, prop):
    columns = get_columns(connection)

    if "project_name_en" not in columns:
        st.error(
            "Database mein project_name_en column nahi mila."
        )
        return None

    candidates = prop["project_candidates"]

    if not candidates:
        return None

    records = {}

    # Search the specific project name first, then broader candidates.
    for candidate in candidates:
        project = normalize(candidate["project"])

        if len(project) < 3:
            continue

        escaped = (
            project.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )

        sql = """
            SELECT *
            FROM units
            WHERE LOWER(COALESCE(project_name_en, ''))
                  LIKE ? ESCAPE '\\'
        """

        params = [f"%{escaped}%"]

        if "master_project_en" in columns:
            sql += """
                OR LOWER(COALESCE(master_project_en, ''))
                   LIKE ? ESCAPE '\\'
            """
            params.append(f"%{escaped}%")

        sql += " LIMIT 3000"

        try:
            found = connection.execute(
                sql,
                params,
            ).fetchall()
        except sqlite3.Error as error:
            st.error(f"Database query failed: {error}")
            return None

        for row in found:
            key = (
                str(get_value(row, ["unit_number"]) or ""),
                str(get_value(row, ["land_number"]) or ""),
                str(get_value(row, ["project_name_en"]) or ""),
            )

            records[key] = row

    if not records:
        return None

    ranked = []

    for row in records.values():
        score = score_record(row, prop)
        ranked.append((score, row))

    ranked.sort(key=lambda item: item[0], reverse=True)

    best_score, best_row = ranked[0]

    # A weak project match must not be shown as a reliable result.
    if best_score < 75:
        return None

    # If the second record is almost equally good, check whether
    # it points to a different unit.
    if len(ranked) > 1:
        second_score, second_row = ranked[1]

        best_unit = str(
            get_value(best_row, ["unit_number"]) or ""
        )

        second_unit = str(
            get_value(second_row, ["unit_number"]) or ""
        )

        # Ambiguity is only meaningful if different units have
        # nearly equal scores. If the scores differ enough, show the top.
        if (
            best_unit != second_unit
            and best_score - second_score < 5
        ):
            return {
                "ambiguous": True,
                "best_score": best_score,
                "row": best_row,
                "second_score": second_score,
            }

    return {
        "ambiguous": False,
        "score": best_score,
        "row": best_row,
    }


# =====================================================
# DISPLAY
# =====================================================

def display_result(result):
    if result is None:
        st.warning(
            "Reliable match nahi mila. Database mein project ke records "
            "hona aur exact listing unit identify hona alag cheezein hain."
        )
        return

    if result["ambiguous"]:
        st.warning(
            "Multiple units ke scores bahut close hain. "
            "App ek record ko confirmed unit nahi keh sakti."
        )

        st.write(
            "**Top candidate score:**",
            result["best_score"],
        )

        st.write(
            "**Second candidate score:**",
            result["second_score"],
        )

        with st.expander("Inspect top candidate"):
            st.json(dict(result["row"]))

        return

    row = result["row"]

    unit = get_value(row, ["unit_number"])
    project = get_value(row, ["project_name_en"])
    master = get_value(row, ["master_project_en"])
    area = get_value(row, ["area_name_en"])
    land = get_value(row, ["land_number"])

    st.success("Top matching DLD record")

    st.metric(
        "Unit Number",
        str(unit) if unit is not None else "N/A",
    )

    st.write("**Project:**", project or "N/A")
    st.write("**Master project:**", master or "N/A")
    st.write("**Area:**", area or "N/A")
    st.write("**Land number:**", land or "N/A")

    st.caption(
        f"Match score: {result['score']} "
        "(ranking score, probability nahi)."
    )

    with st.expander("All DLD fields"):
        st.json(dict(row))


# =====================================================
# SIDEBAR
# =====================================================

with st.sidebar:
    st.subheader("Settings")
    st.caption(f"Repository: {GITHUB_REPO}")

    if st.button("🔄 Update database"):
        if download_database(force=True):
            get_db.clear()
            st.success("Database updated.")
            st.rerun()


# =====================================================
# MAIN
# =====================================================

url = st.text_input(
    "Property Finder URL",
    placeholder="Paste listing URL here",
)

if st.button("Find Unit", type="primary", use_container_width=True):
    if not url.strip():
        st.warning("Property Finder URL enter karein.")

    elif "propertyfinder.ae" not in urlparse(url).netloc.lower():
        st.warning("Valid Property Finder URL enter karein.")

    elif download_database():
        connection = get_db()

        if connection is None:
            st.error("Database open nahi ho saki.")
        else:
            with st.spinner("Searching DLD records..."):
                prop = parse_pf_url(url)
                scraped = scrape_pf_page(url)

                # Only overwrite fields if scraping found real values.
                for key, value in scraped.items():
                    if value not in (None, ""):
                        prop[key] = value

                result = find_best_match(connection, prop)

            with st.expander("Extracted listing details"):
                st.write(
                    "**Project candidates:**",
                    [x["project"] for x in prop["project_candidates"]],
                )
                st.write("**Location:**", prop["location"])
                st.write("**Property type:**", prop["property_type"])
                st.write("**Listing title:**", prop["title"] or "Unavailable")
                st.write("**Bedrooms:**", prop["bedrooms"] or "Unavailable")
                st.write("**Area (sq ft):**", prop["area_sqft"] or "Unavailable")

            st.divider()
            display_result(result)
