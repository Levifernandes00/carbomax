import json
import os
from datetime import datetime
from typing import Any

import psycopg
from playwright.sync_api import sync_playwright

def load_data_via_browser(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # same idea as document.getElementsByTagName("pre")[0].innerHTML
        text = page.locator("pre").first.inner_text()
        browser.close()
        return text

def parse_curve(text: str) -> dict[str, Any]:
    obj = json.loads(text)
    row = obj["TableData"][0]

    test_mode = row["test_mode"]
    material = row["material"]
    lot = row["lot"]
    start_date = datetime.strptime(row["start_date"], "%d%m%Y %H%M%S")
    stop_date = datetime.strptime(row["stop_date"], "%d%m%Y %H%M%S")
    observation = row.get("observation", row.get("obsevation", ""))
    data_temp = row["data_temp"]
    data_deriv = row["data_deriv"]
    module = "MICROSTRUCTURE" if test_mode == "MICROESTRUTURA" else "CARBON"
    batch_name = f"{start_date.strftime('%y.%m')} {lot} GAMMA"

    points = []
    for i, (raw_temp, raw_deriv) in enumerate(zip(data_temp, data_deriv)):
        points.append(
            {
                "point_index": i,
                "seconds": i * 0.5,
                "temperature": raw_temp / 10,
                "derivative": raw_deriv / 100,
            }
        )

    summary_results = [
        {"key": "carbomax_test_mode", "value": None, "obs": test_mode},
        {"key": "carbomax_module", "value": None, "obs": module},
        {"key": "carbomax_material", "value": None, "obs": material},
        {"key": "carbomax_lot", "value": None, "obs": lot},
        {"key": "carbomax_point_count", "value": len(points), "obs": None},
        {
            "key": "carbomax_max_temperature",
            "value": max((p["temperature"] for p in points), default=None),
            "obs": None,
        },
        {
            "key": "carbomax_max_derivative",
            "value": max((p["derivative"] for p in points), default=None),
            "obs": None,
        },
        {
            "key": "carbomax_observation",
            "value": None,
            "obs": observation or None,
        },
    ]

    normalized = {
        "batch": {
            "name": batch_name,
            "date": start_date.date(),
        },
        "device": {
            "name": "Carbomax Delta",
            "identifier": "carbomax-delta",
            "place": "",
            "category": "thermal-analysis",
            "connection_details": {
                "source": "carbomax",
                "mode": module.lower(),
            },
        },
        "measurement": {
            "material": material,
            "lot": lot,
            "test_mode": test_mode,
            "module": module,
            "observation": observation,
            "start_date": start_date,
            "stop_date": stop_date,
        },
        "summary_results": [r for r in summary_results if r["value"] is not None or r["obs"]],
        "points": points,
    }
    return normalized

def extract_numbers_from_histindex(hist_text: str):
    numbers = []
    for line in hist_text.splitlines():
        last = line.split(",")[-1].strip() if "," in line else line.strip()
        if last.replace(".", "", 1).isdigit():
            numbers.append(last)
    return numbers

def ensure_device(cur, company_id: str, payload: dict[str, Any], ip: str) -> int:
    details = payload["connection_details"].copy()
    details["ip"] = ip
    cur.execute(
        """
        INSERT INTO device (name, place, category, connection_details, identifier, company_id)
        VALUES (%s, %s, %s, %s::jsonb, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            payload["name"],
            payload.get("place", ""),
            payload.get("category", ""),
            json.dumps(details),
            payload["identifier"],
            company_id,
        ),
    )
    cur.execute(
        """
        SELECT id
        FROM device
        WHERE identifier = %s AND company_id = %s
        ORDER BY id
        LIMIT 1
        """,
        (payload["identifier"], company_id),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError("Unable to resolve device id")
    return row[0]


def ensure_batch(cur, company_id: str, payload: dict[str, Any]) -> int:
    cur.execute(
        """
        INSERT INTO batch (date, name, day_order, product_id, company_id)
        VALUES (%s, %s, NULL, NULL, %s)
        ON CONFLICT (name, date, company_id) DO NOTHING
        """,
        (payload["date"], payload["name"], company_id),
    )
    cur.execute(
        """
        SELECT id
        FROM batch
        WHERE name = %s AND date = %s AND company_id = %s
        ORDER BY id
        LIMIT 1
        """,
        (payload["name"], payload["date"], company_id),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError("Unable to resolve batch id")
    return row[0]


def store_curve_record(
    conn: psycopg.Connection,
    company_id: str,
    ip: str,
    parsed: dict[str, Any],
):
    with conn.transaction():
        with conn.cursor() as cur:
            device_id = ensure_device(cur, company_id, parsed["device"], ip)
            batch_id = ensure_batch(cur, company_id, parsed["batch"])

            start_date = parsed["measurement"]["start_date"]

            # Idempotency: replace Carbomax-owned summary keys for this batch/device/time.
            cur.execute(
                """
                DELETE FROM result
                WHERE batch_id = %s
                  AND device_id = %s
                  AND datetime = %s
                  AND key LIKE 'carbomax_%%'
                """,
                (batch_id, device_id, start_date),
            )

            result_rows = []
            for item in parsed["summary_results"]:
                result_rows.append(
                    (
                        item["key"],
                        item.get("value"),
                        start_date,
                        item.get("obs"),
                        batch_id,
                        device_id,
                    )
                )
            if result_rows:
                cur.executemany(
                    """
                    INSERT INTO result (key, value, datetime, obs, batch_id, device_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    result_rows,
                )

            # Idempotency: replace all points for this batch/device.
            cur.execute(
                """
                DELETE FROM carbomax_curve_point
                WHERE batch_id = %s AND device_id = %s
                """,
                (batch_id, device_id),
            )

            point_rows = []
            for point in parsed["points"]:
                point_rows.append(
                    (
                        batch_id,
                        device_id,
                        point["point_index"],
                        point["seconds"],
                        point["temperature"],
                        point["derivative"],
                        company_id,
                    )
                )
            if point_rows:
                cur.executemany(
                    """
                    INSERT INTO carbomax_curve_point
                        (batch_id, device_id, point_index, seconds, temperature, derivative, company_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    point_rows,
                )


def ingest_carbomax(ip: str, company_id: str, database_url: str | None = None):
    db_url = database_url or os.getenv("CARBOMAX_DB_URL")
    if not db_url:
        raise ValueError("Missing database URL. Pass database_url or set CARBOMAX_DB_URL")

    hist_text = load_data_via_browser(f"{ip}/HistIndex.dat")
    numbers = extract_numbers_from_histindex(hist_text)

    with psycopg.connect(db_url) as conn:
        for n in numbers:
            try:
                raw = load_data_via_browser(f"{ip}/getdata.cgi?btrqh={n}")
                parsed = parse_curve(raw)
                store_curve_record(conn, company_id, ip, parsed)
                print(f"Stored btrqh={n} ({len(parsed['points'])} points)")
            except Exception as e:
                print(f"Error processing btrqh={n}:", e)