
"""
Convert DLD Units CSV to SQLite database.
Preserves all CSV columns and retries interrupted downloads.
"""

import csv
import sqlite3
import os
import sys
import argparse
import requests
import time
import json
import re
import random
import shutil

CSV_URL = (
    "https://www.dubaipulse.gov.ae/dataset/"
    "85462a5b-08dc-4325-9242-676a0de4afc4/resource/"
    "7d4deadf-c9bc-47a4-85de-998d0ce38bf3/download/units.csv"
)

DB_PATH = "dld_units.db"
CSV_RAW = "units_raw.csv"
METADATA_PATH = "db_metadata.json"

# Download settings
MAX_RETRIES = 8
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 180
CHUNK_SIZE = 1024 * 1024

INDEX_COLUMNS = [
    "project_name_en", "area_name_en", "master_project_en",
    "unit_number", "land_number", "building_number",
    "property_type_en", "rooms", "zone_id", "area_id",
    "property_id", "parent_property_id", "grandparent_property_id",
    "is_free_hold", "project_name_ar", "area_name_ar",
    "master_project_ar",
]


def download_csv(output_path=CSV_RAW):
    """Download CSV with retries and resume support."""

    print("=" * 60)
    print("STEP 1: Downloading CSV from Dubai Pulse")
    print("=" * 60)
    print(f"URL: {CSV_URL}")
    print("Large file: download may take some time.\n")

    start = time.time()
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 DLD-Unit-Finder/1.0",
        "Accept-Encoding": "identity",
    })

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        response = None

        try:
            offset = (
                os.path.getsize(output_path)
                if os.path.exists(output_path)
                else 0
            )

            headers = {}
            if offset > 0:
                headers["Range"] = f"bytes={offset}-"
                print(
                    f"\nResuming existing download at "
                    f"{offset / (1024 * 1024):.1f} MB"
                )
            else:
                print("\nStarting download from the beginning.")

            print(f"Attempt {attempt}/{MAX_RETRIES}")

            response = session.get(
                CSV_URL,
                headers=headers,
                stream=True,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                allow_redirects=True,
            )

            # If the server ignores Range, restart safely.
            if offset > 0 and response.status_code == 200:
                print("Server does not support resume; restarting download.")
                offset = 0

            elif offset > 0 and response.status_code == 416:
                # The partial file might already be complete.
                content_range = response.headers.get("Content-Range", "")
                match = re.search(r"\*/(\d+)", content_range)

                response.close()
                response = None

                if match and offset == int(match.group(1)):
                    print("Existing file appears complete.")
                    return output_path

                print("Resume position rejected; restarting download.")
                if os.path.exists(output_path):
                    os.remove(output_path)
                time.sleep(min(attempt * 3, 20))
                continue

            response.raise_for_status()

            # Validate partial response.
            if offset > 0 and response.status_code == 206:
                content_range = response.headers.get("Content-Range", "")
                match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range)

                if not match or int(match.group(1)) != offset:
                    raise RuntimeError(
                        f"Invalid Content-Range response: {content_range}"
                    )

            elif response.status_code not in (200, 206):
                raise RuntimeError(
                    f"Unexpected HTTP status: {response.status_code}"
                )

            # A 206 response should start at the requested offset.
            if response.status_code == 206 and offset == 0:
                content_range = response.headers.get("Content-Range", "")
                match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range)
                if not match or int(match.group(1)) != 0:
                    raise RuntimeError(
                        f"Unexpected partial response: {content_range}"
                    )

            # Determine total file size where available.
            total_size = 0
            content_range = response.headers.get("Content-Range", "")
            match = re.match(r"bytes\s+\d+-\d+/(\d+)", content_range)

            if match:
                total_size = int(match.group(1))
            else:
                content_length = response.headers.get("Content-Length")
                if content_length:
                    total_size = offset + int(content_length)

            mode = "ab" if offset > 0 else "wb"
            downloaded = offset
            last_report = time.time()

            with open(output_path, mode) as file:
                for chunk in response.iter_content(
                    chunk_size=CHUNK_SIZE
                ):
                    if not chunk:
                        continue

                    file.write(chunk)
                    downloaded += len(chunk)

                    if time.time() - last_report >= 2:
                        elapsed = max(time.time() - start, 1)
                        speed = downloaded / (1024 * 1024) / elapsed

                        if total_size:
                            pct = downloaded * 100 / total_size
                            print(
                                f"\rDownloaded {downloaded / 1048576:.1f} MB "
                                f"/ {total_size / 1048576:.1f} MB "
                                f"({pct:.1f}%) | {speed:.2f} MB/s",
                                end="",
                                flush=True,
                            )
                        else:
                            print(
                                f"\rDownloaded {downloaded / 1048576:.1f} MB "
                                f"| {speed:.2f} MB/s",
                                end="",
                                flush=True,
                            )

                        last_report = time.time()

            response.close()
            response = None

            actual_size = os.path.getsize(output_path)

            # Check that the advertised file length was received.
            if total_size and actual_size != total_size:
                raise IOError(
                    f"Incomplete download: {actual_size} bytes received; "
                    f"expected {total_size} bytes"
                )

            if actual_size == 0:
                raise IOError("Downloaded file is empty.")

            elapsed = time.time() - start
            print(
                f"\n\nDownload complete: "
                f"{actual_size / 1048576:.1f} MB in {elapsed:.0f}s\n"
            )

            return output_path

        except (requests.RequestException, OSError, RuntimeError) as exc:
            last_error = exc
            print(f"\nDownload attempt failed: {type(exc).__name__}: {exc}")

            if attempt < MAX_RETRIES:
                # Preserve the partial file when possible for resuming.
                delay = min(15 * attempt, 90) + random.uniform(0, 3)
                print(f"Retrying in {delay:.0f} seconds...")
                time.sleep(delay)

        finally:
            if response is not None:
                response.close()

    session.close()
    raise RuntimeError(
        f"Download failed after {MAX_RETRIES} attempts. "
        f"Last error: {last_error}"
    )


def sanitize_column_name(name):
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip())
    safe = re.sub(r"_+", "_", safe).strip("_").lower()
    return safe if safe else "col_unknown"


def convert_csv_to_sqlite(csv_path, db_path=DB_PATH):
    print("=" * 60)
    print("STEP 2: Converting CSV to SQLite")
    print("=" * 60)
    print(
        f"Input: {csv_path} "
        f"({os.path.getsize(csv_path) / 1048576:.1f} MB)"
    )
    print(f"Output: {db_path}\n")

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        raw_columns = [col.strip() for col in next(csv.reader(f))]

    safe_columns = [sanitize_column_name(c) for c in raw_columns]

    seen = {}
    unique_columns = []

    for col in safe_columns:
        if col in seen:
            seen[col] += 1
            unique_columns.append(f"{col}_{seen[col]}")
        else:
            seen[col] = 0
            unique_columns.append(col)

    print(f"Detected {len(unique_columns)} columns.")

    if os.path.exists(db_path):
        os.remove(db_path)

    conn = sqlite3.connect(db_path)

    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -64000")
        cursor = conn.cursor()

        col_defs = [f'"{col}" TEXT' for col in unique_columns]
        cursor.execute(
            f'CREATE TABLE units ({", ".join(col_defs)})'
        )

        placeholders = ", ".join("?" for _ in unique_columns)
        col_list = ", ".join(f'"{c}"' for c in unique_columns)
        insert_sql = (
            f"INSERT INTO units ({col_list}) "
            f"VALUES ({placeholders})"
        )

        csv.field_size_limit(sys.maxsize)

        start = time.time()
        total_rows = 0
        error_rows = 0
        empty_rows = 0
        batch = []

        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            next(reader)

            for line_num, row in enumerate(reader, start=2):
                try:
                    if len(row) < len(unique_columns):
                        row.extend(
                            [""] * (len(unique_columns) - len(row))
                        )
                    elif len(row) > len(unique_columns):
                        raise ValueError(
                            f"Row has {len(row)} fields; expected "
                            f"{len(unique_columns)}. Refusing to truncate."
                        )

                    if all(cell.strip() == "" for cell in row):
                        empty_rows += 1
                        continue

                    batch.append(tuple(cell.strip() for cell in row))
                    total_rows += 1

                    if len(batch) >= 25000:
                        cursor.executemany(insert_sql, batch)
                        conn.commit()
                        batch.clear()

                        elapsed = max(time.time() - start, 1)
                        print(
                            f"\rRows: {total_rows:,} "
                            f"({total_rows / elapsed:,.0f}/sec) "
                            f"Errors: {error_rows} Empty: {empty_rows}",
                            end="",
                            flush=True,
                        )

                except Exception as exc:
                    error_rows += 1
                    if error_rows <= 10:
                        print(f"\nRow {line_num}: {exc}")

            if batch:
                cursor.executemany(insert_sql, batch)
                conn.commit()

        print("\n\nCreating indexes...")
        actual_indexes = []

        for col in INDEX_COLUMNS:
            if col in unique_columns:
                idx = f"idx_{col}"
                collate = " COLLATE NOCASE" if col.endswith(("_en", "_ar")) else ""

                cursor.execute(
                    f'CREATE INDEX IF NOT EXISTS "{idx}" '
                    f'ON units("{col}"{collate})'
                )
                actual_indexes.append(col)
                print(f"  Created {idx}")

        print("Running ANALYZE and VACUUM...")
        cursor.execute("ANALYZE")
        conn.commit()
        cursor.execute("VACUUM")
        conn.commit()

        db_size = os.path.getsize(db_path) / 1048576
        csv_size = os.path.getsize(csv_path) / 1048576
        elapsed = time.time() - start

        metadata = {
            "source_url": CSV_URL,
            "csv_size_mb": round(csv_size, 1),
            "db_size_mb": round(db_size, 1),
            "columns": unique_columns,
            "column_count": len(unique_columns),
            "total_rows": total_rows,
            "empty_rows_skipped": empty_rows,
            "error_rows": error_rows,
            "indexes": actual_indexes,
            "created_at": time.strftime(
                "%Y-%m-%d %H:%M:%S UTC", time.gmtime()
            ),
        }

        with open(METADATA_PATH, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

        print("\n" + "=" * 60)
        print("CONVERSION COMPLETE")
        print("=" * 60)
        print(f"Columns: {len(unique_columns)}")
        print(f"Rows: {total_rows:,}")
        print(f"Empty rows skipped: {empty_rows:,}")
        print(f"Errors: {error_rows:,}")
        print(f"Indexes: {len(actual_indexes)}")
        print(f"Database size: {db_size:.1f} MB")
        print(f"Time: {elapsed:.0f}s")
        print(f"Metadata: {METADATA_PATH}")

        return db_path, metadata

    finally:
        conn.close()


def verify_db(db_path=DB_PATH, csv_path=None):
    print("\n" + "=" * 60)
    print("DATABASE VERIFICATION")
    print("=" * 60)

    if not os.path.exists(db_path):
        print("Database not found.")
        return False

    conn = sqlite3.connect(db_path)

    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check")
        integrity = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM units")
        db_rows = cursor.fetchone()[0]

        cursor.execute("PRAGMA table_info(units)")
        db_cols = cursor.fetchall()

        cursor.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
        )
        index_count = cursor.fetchone()[0]

        print(f"Integrity: {integrity}")
        print(f"Rows: {db_rows:,}")
        print(f"Columns: {len(db_cols)}")
        print(f"Indexes: {index_count}")

        if integrity != "ok":
            return False

        if csv_path and os.path.exists(csv_path):
            csv_rows = 0

            with open(
                csv_path, "r", encoding="utf-8-sig", newline=""
            ) as f:
                reader = csv.reader(f)
                csv_header = next(reader)

                for row in reader:
                    if any(cell.strip() for cell in row):
                        csv_rows += 1

            print(f"CSV rows: {csv_rows:,}")
            print(f"DB rows: {db_rows:,}")

            if (
                csv_rows == db_rows
                and len(csv_header) == len(db_cols)
            ):
                print("CSV and DB row/column counts match.")
            else:
                print("WARNING: CSV and DB counts differ.")
                return False

        return True

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="DLD CSV to SQLite converter"
    )
    parser.add_argument("--csv", help="Use an existing local CSV")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    print("\nDLD Unit Finder — CSV to SQLite\n")

    if args.verify:
        success = verify_db(args.db)
        sys.exit(0 if success else 1)

    if args.csv:
        csv_path = args.csv
        if not os.path.exists(csv_path):
            print(f"CSV file not found: {csv_path}")
            sys.exit(1)
    else:
        csv_path = download_csv()

    db_path, metadata = convert_csv_to_sqlite(csv_path, args.db)

    if not verify_db(db_path, csv_path):
        print("Verification failed.")
        sys.exit(1)

    if not args.csv and os.path.exists(CSV_RAW):
        os.remove(CSV_RAW)

    print(
        f"\nDONE: {db_path} "
        f"({metadata['db_size_mb']} MB, "
        f"{metadata['total_rows']:,} rows, "
        f"{metadata['column_count']} columns)"
    )


if __name__ == "__main__":
    main()
