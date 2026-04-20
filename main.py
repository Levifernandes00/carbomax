import json
import os
import time
from datetime import datetime
from pathlib import Path
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
    peak_raw = row.get("peak") / 10
    tl_raw = row.get("liquidus") / 10
    ce_raw = row.get("carbon_eq") / 100
    tse_raw = row.get("tse") / 10
    tre_raw = row.get("tre") / 10
    rec_raw = row.get("recalec") / 10
    d_rec_raw = row.get("delta_rec")
    final_raw = row.get("final") / 10
    module = "MICROSTRUCTURE" if test_mode == "MICROESTRUTURA" else "CARBON"
    batch_name = f"{start_date.strftime('%y.%m')} {lot} GAMMA"

    tse_value = None
    tse_obs = None
    if tse_raw is not None and str(tse_raw).strip() != "":
        try:
            tse_value = float(str(tse_raw).replace(",", "."))
        except ValueError:
            tse_obs = str(tse_raw)

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
        {"key": "peak", "value": peak_raw, "obs": observation},
        {"key": "liquidus", "value": tl_raw, "obs": observation},
        {"key": "carbon_eq", "value": ce_raw, "obs": observation},
        {"key": "tse", "value": tse_raw, "obs": observation},
        {"key": "tre", "value": tre_raw, "obs": observation},
        {"key": "recalec", "value": rec_raw, "obs": observation},
        {"key": "delta_rec", "value": d_rec_raw, "obs": observation},
        {"key": "final", "value": final_raw, "obs": observation},
    ]

    print(summary_results)
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
        "results": [r for r in summary_results],
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
    persisted_tse = None
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

            # Keep TSE idempotent for this batch/device/timestamp as well.
            cur.execute(
                """
                DELETE FROM result
                WHERE batch_id = %s
                  AND device_id = %s
                  AND datetime = %s
                  AND key = 'TSE'
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

            tse_raw = parsed["measurement"].get("tse_raw")
            if tse_raw is not None and str(tse_raw).strip() != "":
                tse_value = parsed["measurement"].get("tse_value")
                tse_obs = parsed["measurement"].get("tse_obs")
                cur.execute(
                    """
                    INSERT INTO result (key, value, datetime, obs, batch_id, device_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    ("TSE", tse_value, start_date, tse_obs, batch_id, device_id),
                )
                persisted_tse = {
                    "batch_name": parsed["batch"]["name"],
                    "device_id": device_id,
                    "datetime": start_date.isoformat(),
                    "value": tse_value,
                    "obs": tse_obs,
                }

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
    return persisted_tse


def append_tse_log(event: dict[str, Any], file_path: str | None = None):
    if not event:
        return
    target = file_path or os.getenv(
        "CARBOMAX_TSE_LOG_FILE",
        str(Path(__file__).with_name("tse_results.log")),
    )
    line = (
        f"{datetime.now().isoformat()} | batch={event.get('batch_name')} | "
        f"device_id={event.get('device_id')} | "
        f"datetime={event.get('datetime')} | "
        f"value={event.get('value')} | obs={event.get('obs') or ''}\n"
    )
    with open(target, "a", encoding="utf-8") as f:
        f.write(line)


def ingest_carbomax(ip: str, company_id: str, database_url: str | None = None):
    db_url = database_url or os.getenv("CARBOMAX_DB_URL")
    if not db_url:
        raise ValueError("Missing database URL. Pass database_url or set CARBOMAX_DB_URL")

    hist_text = load_data_via_browser(f"{ip}/HistIndex.dat")
    numbers = extract_numbers_from_histindex(hist_text)

    with psycopg.connect(db_url) as conn:
        for n in numbers:
            try:
                process_btrqh(conn, ip, company_id, n)
            except Exception as e:
                print(f"Error processing btrqh={n}:", e)


def process_btrqh(conn: psycopg.Connection, ip: str, company_id: str, btrqh: str):
    raw = load_data_via_browser(f"{ip}/getdata.cgi?btrqh={btrqh}")
    parsed = parse_curve(raw)
    tse_event = store_curve_record(conn, company_id, ip, parsed)
    append_tse_log(tse_event)
    # Terminal bell + message to alert a new acquisition.
    print(f"\aStored btrqh={btrqh} ({len(parsed['points'])} points)")


def run_polling_loop(
    ip: str,
    company_id: str,
    database_url: str | None = None,
    interval_seconds: int = 5,
    max_cycles: int | None = None,
    sleep_fn=time.sleep,
):
    db_url = database_url or os.getenv("CARBOMAX_DB_URL")
    if not db_url:
        raise ValueError("Missing database URL. Pass database_url or set CARBOMAX_DB_URL")

    seen_btrqh: set[str] = set()
    print(f"[carbomax] starting polling loop every {interval_seconds}s for {ip}")

    cycle_count = 0
    with psycopg.connect(db_url) as conn:
        try:
            while True:
                cycle_count += 1
                cycle_started = time.time()
                try:
                    hist_text = load_data_via_browser(f"{ip}/HistIndex.dat")
                    numbers = extract_numbers_from_histindex(hist_text)
                except Exception as e:
                    print(f"[carbomax] cycle fetch error: {e}")
                    numbers = []

                for btrqh in numbers:
                    if btrqh in seen_btrqh:
                        continue
                    try:
                        process_btrqh(conn, ip, company_id, btrqh)
                        seen_btrqh.add(btrqh)
                    except Exception as e:
                        print(f"[carbomax] error processing btrqh={btrqh}: {e}")

                elapsed = time.time() - cycle_started
                sleep_for = max(0.0, float(interval_seconds) - elapsed)
                if max_cycles is not None and cycle_count >= max_cycles:
                    break
                sleep_fn(sleep_for)
        except KeyboardInterrupt:
            print("\n[carbomax] polling loop stopped by user.")