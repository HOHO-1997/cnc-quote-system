"""SQLite 历史报价与实际工时校准；旧 quotes.db 自动加列，不删除数据。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

DB_FILE = Path(__file__).parent / "quotes.db"

COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT", "quote_date": "TEXT", "customer": "TEXT", "product_name": "TEXT", "product_number": "TEXT",
    "quantity": "INTEGER", "material": "TEXT", "weight": "REAL", "cnc_hours": "REAL", "surface_treatment": "TEXT", "packaging_cost": "REAL",
    "material_cost": "REAL", "cnc_cost": "REAL", "surface_cost": "REAL", "total_cost": "REAL", "profit_multiplier": "REAL", "final_price": "REAL", "cnc_details": "TEXT",
    "quote_mode": "TEXT", "unit_cost": "REAL", "unit_price": "REAL", "batch_cost": "REAL", "batch_price": "REAL", "one_time_cost": "REAL",
    "product_type": "TEXT", "fixture_count": "INTEGER", "predicted_cnc": "REAL", "actual_cnc": "REAL", "predicted_lathe": "REAL", "actual_lathe": "REAL",
    "predicted_gantry": "REAL", "actual_gantry": "REAL", "predicted_grinding": "REAL", "actual_grinding": "REAL", "actual_labor": "REAL", "actual_total_cost": "REAL", "production_note": "TEXT",
}


def init_database() -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS quotes (" + ", ".join(f"{name} {kind}" for name, kind in COLUMNS.items()) + ")")
        existing = {row[1] for row in conn.execute("PRAGMA table_info(quotes)")}
        for name, kind in COLUMNS.items():
            if name not in existing and name != "id": conn.execute(f"ALTER TABLE quotes ADD COLUMN {name} {kind}")


def save_quote(data: dict, result: dict, config: dict) -> None:
    hours = {row["推荐设备"]: 0.0 for row in result["confirmed_rows"]}
    for row in result["confirmed_rows"]: hours[row["推荐设备"]] = hours.get(row["推荐设备"], 0) + float(row["推荐时间(h)"])
    values = {"quote_date": datetime.now().strftime("%Y-%m-%d %H:%M"), "customer": data.get("customer", ""), "product_name": data.get("product_name", ""),
              "product_number": data.get("product_number", ""), "quantity": data.get("quantity", 1), "material": data.get("material", ""), "weight": data.get("net_weight", 0),
              "cnc_hours": sum(hours.values()), "surface_treatment": json.dumps(data.get("surfaces", []), ensure_ascii=False), "packaging_cost": data.get("packaging_cost", 0),
              "material_cost": result["casting_per_unit"], "cnc_cost": result["equipment_per_unit"], "surface_cost": result["surface_per_unit"], "total_cost": result["unit_cost"],
              "profit_multiplier": config.get("profit_multiplier", 1.2), "final_price": result["unit_price"], "cnc_details": json.dumps(result["confirmed_rows"], ensure_ascii=False),
              "quote_mode": result["quote_mode"], "unit_cost": result["unit_cost"], "unit_price": result["unit_price"], "batch_cost": result["batch_cost"], "batch_price": result["batch_price"],
              "one_time_cost": result["one_time_cost"], "product_type": data.get("product_type", "自动识别"), "fixture_count": data.get("fixture_count", 0),
              "predicted_cnc": hours.get("CNC加工中心", 0), "predicted_lathe": hours.get("车床", 0), "predicted_gantry": hours.get("龙门铣", 0), "predicted_grinding": hours.get("磨床", 0)}
    names = list(values); placeholders = ",".join("?" for _ in names)
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(f"INSERT INTO quotes ({','.join(names)}) VALUES ({placeholders})", [values[n] for n in names])


def history() -> pd.DataFrame:
    with sqlite3.connect(DB_FILE) as conn:
        return pd.read_sql_query("SELECT * FROM quotes ORDER BY id DESC", conn)


def update_actual(record_id: int, values: dict) -> None:
    allowed = {k: v for k, v in values.items() if k in COLUMNS and k != "id"}
    if not allowed: return
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE quotes SET " + ", ".join(f"{k}=?" for k in allowed) + " WHERE id=?", [*allowed.values(), record_id])


def calibration(product_type: str) -> dict:
    df = history()
    if df.empty or not product_type: return {"factor": 1.0, "samples": []}
    df = df[df["product_type"].fillna("") == product_type].copy()
    pairs = [("predicted_cnc", "actual_cnc"), ("predicted_lathe", "actual_lathe"), ("predicted_gantry", "actual_gantry"), ("predicted_grinding", "actual_grinding")]
    predicted = sum(pd.to_numeric(df[a], errors="coerce").fillna(0) for a, _ in pairs)
    actual = sum(pd.to_numeric(df[b], errors="coerce").fillna(0) for _, b in pairs)
    valid = predicted > 0
    ratios = (actual[valid] / predicted[valid]).replace([float("inf")], float("nan")).dropna()
    return {"factor": float(ratios.mean()) if not ratios.empty else 1.0,
            "samples": df[["product_name", "predicted_cnc", "actual_cnc", "predicted_lathe", "actual_lathe", "production_note"]].head(5).to_dict("records")}
