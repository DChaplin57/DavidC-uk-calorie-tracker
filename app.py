"""UK Calorie Tracker (Lose It!-style) — Streamlit app (Upgraded)

What’s included
- ✅ Daily diary (meals), calorie budget, remaining calories
- ✅ Fast search + filters across your Excel food DB
- ✅ Log by grams or by typical portion
- ✅ Custom recipes (build from ingredients, set servings, log servings)
- ✅ Saved meals (templates you can re-log in one click)
- ✅ Barcode lookup (Open Food Facts) + cache (manual barcode entry, optional camera upload if pyzbar installed)
- ✅ Macro targets (protein/carbs/fat) + macro tracking when data is available
- ✅ Weight tracking (kg) + weekly trend charts

How to run
1) Put this file as app.py
2) Put your Excel file in same folder (or set FOOD_DB_PATH)
3) Install deps:
   pip install streamlit pandas openpyxl requests
   # Optional for barcode-from-image:
   pip install pillow pyzbar
4) Run:
   python3 -m streamlit run app.py

Notes on macros
- Your current Excel DB focuses on calories; it may not include macros.
- This app can still track macros if:
  a) macros exist in the Excel sheet (Protein/Fat/Carbs columns), OR
  b) you log packaged foods via barcode lookup (Open Food Facts), OR
  c) you set macro overrides for specific foods inside the app.

Data storage
- Diary / recipes / saved meals / barcode cache / weight logs stored locally in SQLite.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Optional, Tuple

import pandas as pd
import streamlit as st

# External (network)
import requests


# ----------------------------
# Configuration
# ----------------------------
DEFAULT_DB_PATH = os.environ.get(
    "FOOD_DB_PATH",
    "UK_store_cupboard_ingredients_calories_with_portions.xlsx",
)
SQLITE_PATH = os.environ.get("DIARY_DB_PATH", "calorie_diary.sqlite")

MEALS = ["Breakfast", "Lunch", "Dinner", "Snacks"]

# Open Food Facts (v2 API)
OFF_PRODUCT_URL = "https://world.openfoodfacts.net/api/v2/product/{barcode}"


# ----------------------------
# Utilities
# ----------------------------

def safe_float(x, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def kcal_for_grams(kcal_per_100g: float, grams: float) -> float:
    return float(kcal_per_100g) * float(grams) / 100.0


def grams_from_ml_approx(ml: float) -> float:
    # Very rough: assume water-like density.
    return float(ml)


# ----------------------------
# Data loading & normalization
# ----------------------------
@st.cache_data(show_spinner=False)
def load_food_database(xlsx_path: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (ingredients_df, portions_df)."""
    ingredients = pd.read_excel(xlsx_path, sheet_name="Ingredients")
    portions = pd.read_excel(xlsx_path, sheet_name="Per item portions")

    # Normalize expected columns
    ingredients["Item"] = ingredients["Item"].astype(str)
    for col in ["CuisineTags", "MainCategory", "Subcategory", "ShelfStable", "Basis"]:
        if col in ingredients.columns:
            ingredients[col] = ingredients[col].fillna("")

    # Optional macros columns (if present)
    # These names are flexible; we standardize to Protein/Fat/Carbs per 100g
    macro_map = {
        "Protein (g)": "Protein_g_100g",
        "Protein (g/100g)": "Protein_g_100g",
        "Protein_g_100g": "Protein_g_100g",
        "Fat (g)": "Fat_g_100g",
        "Fat (g/100g)": "Fat_g_100g",
        "Fat_g_100g": "Fat_g_100g",
        "Carbohydrate (g)": "Carbs_g_100g",
        "Carbohydrates (g)": "Carbs_g_100g",
        "Carb (g)": "Carbs_g_100g",
        "Carbs_g_100g": "Carbs_g_100g",
        "Carbohydrate_g_100g": "Carbs_g_100g",
    }
    for src, dst in macro_map.items():
        if src in ingredients.columns and dst not in ingredients.columns:
            ingredients[dst] = ingredients[src]

    # Join keys
    ingredients["_key"] = ingredients["Item"].str.strip().str.lower()

    portions["Item (matched to sheet 1)"] = portions["Item (matched to sheet 1)"].astype(str)
    portions["Portion"] = portions["Portion"].astype(str)
    portions["_key"] = portions["Item (matched to sheet 1)"].str.strip().str.lower()

    return ingredients, portions


# ----------------------------
# SQLite persistence (with migrations)
# ----------------------------

# ----------------------------
# JSON backup/restore helpers (mobile-friendly)
# ----------------------------

def _sqlite_table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    info = conn.execute(f"PRAGMA table_info({table});").fetchall()
    return [str(r[1]) for r in info]


def make_json_backup(conn: sqlite3.Connection) -> dict:
    """Dump all user-data tables to a JSON-serializable dict."""
    tables = [
        "diary_entries",
        "burn_entries",
        "food_macro_overrides",
        "recipes",
        "recipe_items",
        "saved_meals",
        "saved_meal_items",
        "barcode_cache",
        "weights",
        "favourites",
    ]

    out: dict = {
        "meta": {
            "app": "UK Calorie Tracker",
            "backup_version": 1,
            "created_at_utc": datetime.utcnow().isoformat(timespec="seconds"),
        },
        "tables": {},
    }

    for t in tables:
        try:
            cols = _sqlite_table_columns(conn, t)
            rows = conn.execute(f"SELECT * FROM {t};").fetchall()
            out["tables"][t] = {
                "columns": cols,
                "rows": [dict(r) for r in rows],
            }
        except Exception:
            # If a table doesn't exist yet, store it empty
            out["tables"][t] = {"columns": [], "rows": []}

    return out


def restore_json_backup(conn: sqlite3.Connection, backup: dict) -> None:
    """Restore from a backup created by make_json_backup().

    This overwrites current data in the listed tables.
    """
    tables = backup.get("tables") or {}
    if not isinstance(tables, dict):
        raise ValueError("Invalid backup format: 'tables' missing")

    # Ensure schema exists
    _ = get_conn()

    conn.execute("BEGIN;")
    try:
        # Delete in dependency-safe order (children first)
        delete_order = [
            "recipe_items",
            "recipes",
            "saved_meal_items",
            "saved_meals",
            "diary_entries",
            "burn_entries",
            "food_macro_overrides",
            "barcode_cache",
            "weights",
            "favourites",
        ]
        for t in delete_order:
            try:
                conn.execute(f"DELETE FROM {t};")
            except Exception:
                pass

        # Insert in parent-first order
        insert_order = [
            "recipes",
            "recipe_items",
            "saved_meals",
            "saved_meal_items",
            "diary_entries",
            "burn_entries",
            "food_macro_overrides",
            "barcode_cache",
            "weights",
            "favourites",
        ]

        for t in insert_order:
            tdata = tables.get(t) or {}
            rows = tdata.get("rows") or []
            if not rows:
                continue

            # Only insert columns that exist in current schema
            current_cols = set(_sqlite_table_columns(conn, t))
            # Maintain stable column order
            cols = [c for c in (tdata.get("columns") or []) if c in current_cols]
            if not cols:
                # Fallback: derive columns from first row
                cols = [c for c in rows[0].keys() if c in current_cols]

            placeholders = ",".join(["?"] * len(cols))
            col_sql = ",".join(cols)
            sql = f"INSERT INTO {t}({col_sql}) VALUES ({placeholders});"

            for r in rows:
                vals = [r.get(c) for c in cols]
                conn.execute(sql, vals)

        conn.commit()
    except Exception:
        conn.execute("ROLLBACK;")
        raise


# ----------------------------
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Core diary
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS diary_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_date TEXT NOT NULL,
            meal TEXT NOT NULL,
            item TEXT NOT NULL,
            grams REAL NOT NULL,
            kcal_per_100g REAL NOT NULL,
            kcal REAL NOT NULL,
            protein_g REAL,
            carbs_g REAL,
            fat_g REAL,
            source TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_diary_date ON diary_entries(entry_date);")

    # Calories burned (exercise / activity / daily living)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS burn_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_date TEXT NOT NULL,
            category TEXT NOT NULL,          -- Exercise | Activity | Daily living
            name TEXT NOT NULL,
            kcal_burned REAL NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_burn_date ON burn_entries(entry_date);")

    # Macro overrides for items (per 100g)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS food_macro_overrides (
            item_key TEXT PRIMARY KEY,
            protein_g_100g REAL,
            carbs_g_100g REAL,
            fat_g_100g REAL,
            updated_at TEXT NOT NULL
        )
        """
    )

    # Recipes
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            servings REAL NOT NULL,
            notes TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recipe_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL,
            item TEXT NOT NULL,
            grams REAL NOT NULL,
            kcal_per_100g REAL NOT NULL,
            kcal REAL NOT NULL,
            protein_g REAL,
            carbs_g REAL,
            fat_g REAL,
            source TEXT,
            FOREIGN KEY(recipe_id) REFERENCES recipes(id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_recipe_items_recipe ON recipe_items(recipe_id);")

    # Saved meals (templates)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS saved_meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            meal TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS saved_meal_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            saved_meal_id INTEGER NOT NULL,
            item TEXT NOT NULL,
            grams REAL NOT NULL,
            kcal_per_100g REAL NOT NULL,
            kcal REAL NOT NULL,
            protein_g REAL,
            carbs_g REAL,
            fat_g REAL,
            source TEXT,
            FOREIGN KEY(saved_meal_id) REFERENCES saved_meals(id)
        )
        """
    )

    # Barcode cache
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS barcode_cache (
            barcode TEXT PRIMARY KEY,
            product_name TEXT,
            kcal_per_100g REAL,
            protein_g_100g REAL,
            carbs_g_100g REAL,
            fat_g_100g REAL,
            raw_json TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )

    # Weight logs
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weights (
            entry_date TEXT PRIMARY KEY,
            weight_kg REAL NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    return conn


# ----------------------------
# Diary operations
# ----------------------------

def add_diary_entry(
    conn: sqlite3.Connection,
    entry_date: date,
    meal: str,
    item: str,
    grams: float,
    kcal_per_100g: float,
    kcal: float,
    protein_g: Optional[float] = None,
    carbs_g: Optional[float] = None,
    fat_g: Optional[float] = None,
    source: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO diary_entries(entry_date, meal, item, grams, kcal_per_100g, kcal, protein_g, carbs_g, fat_g, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry_date.isoformat(),
            meal,
            item,
            float(grams),
            float(kcal_per_100g),
            float(kcal),
            None if protein_g is None else float(protein_g),
            None if carbs_g is None else float(carbs_g),
            None if fat_g is None else float(fat_g),
            source,
            datetime.utcnow().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def delete_diary_entry(conn: sqlite3.Connection, entry_id: int) -> None:
    conn.execute("DELETE FROM diary_entries WHERE id = ?", (int(entry_id),))
    conn.commit()


def read_entries(conn: sqlite3.Connection, entry_date: date) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM diary_entries WHERE entry_date = ? ORDER BY meal, id",
        conn,
        params=(entry_date.isoformat(),),
    )


def read_entries_range(conn: sqlite3.Connection, start: date, end: date) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT * FROM diary_entries
        WHERE entry_date >= ? AND entry_date <= ?
        ORDER BY entry_date, meal, id
        """,
        conn,
        params=(start.isoformat(), end.isoformat()),
    )


# ----------------------------
# Burn (exercise / activity / daily living)
# ----------------------------

def add_burn_entry(
    conn: sqlite3.Connection,
    entry_date: date,
    category: str,
    name: str,
    kcal_burned: float,
) -> None:
    conn.execute(
        """
        INSERT INTO burn_entries(entry_date, category, name, kcal_burned, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            entry_date.isoformat(),
            category,
            name,
            float(kcal_burned),
            datetime.utcnow().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def delete_burn_entry(conn: sqlite3.Connection, burn_id: int) -> None:
    conn.execute("DELETE FROM burn_entries WHERE id = ?", (int(burn_id),))
    conn.commit()


def read_burn_entries(conn: sqlite3.Connection, entry_date: date) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM burn_entries WHERE entry_date = ? ORDER BY category, id",
        conn,
        params=(entry_date.isoformat(),),
    )


def read_burn_entries_range(conn: sqlite3.Connection, start: date, end: date) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT * FROM burn_entries
        WHERE entry_date >= ? AND entry_date <= ?
        ORDER BY entry_date, category, id
        """,
        conn,
        params=(start.isoformat(), end.isoformat()),
    )


# ----------------------------
# Macros: overrides + computation
# ----------------------------

def upsert_macro_override(conn: sqlite3.Connection, item_key: str, p: float, c: float, f: float) -> None:
    conn.execute(
        """
        INSERT INTO food_macro_overrides(item_key, protein_g_100g, carbs_g_100g, fat_g_100g, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(item_key) DO UPDATE SET
            protein_g_100g=excluded.protein_g_100g,
            carbs_g_100g=excluded.carbs_g_100g,
            fat_g_100g=excluded.fat_g_100g,
            updated_at=excluded.updated_at
        """,
        (item_key, float(p), float(c), float(f), datetime.utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()


def get_macro_override(conn: sqlite3.Connection, item_key: str) -> Optional[Dict[str, float]]:
    cur = conn.execute(
        "SELECT protein_g_100g, carbs_g_100g, fat_g_100g FROM food_macro_overrides WHERE item_key = ?",
        (item_key,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "Protein_g_100g": safe_float(row[0], 0.0),
        "Carbs_g_100g": safe_float(row[1], 0.0),
        "Fat_g_100g": safe_float(row[2], 0.0),
    }


def macros_for_grams(protein_100g: Optional[float], carbs_100g: Optional[float], fat_100g: Optional[float], grams: float):
    if protein_100g is None and carbs_100g is None and fat_100g is None:
        return None, None, None
    p = None if protein_100g is None else float(protein_100g) * float(grams) / 100.0
    c = None if carbs_100g is None else float(carbs_100g) * float(grams) / 100.0
    f = None if fat_100g is None else float(fat_100g) * float(grams) / 100.0
    return p, c, f


# ----------------------------
# Recipes
# ----------------------------

def create_recipe(conn: sqlite3.Connection, name: str, servings: float, notes: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO recipes(name, servings, notes, created_at) VALUES (?, ?, ?, ?)",
        (name, float(servings), notes, datetime.utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()
    return int(cur.lastrowid)


def add_recipe_item(
    conn: sqlite3.Connection,
    recipe_id: int,
    item: str,
    grams: float,
    kcal_per_100g: float,
    kcal: float,
    protein_g: Optional[float],
    carbs_g: Optional[float],
    fat_g: Optional[float],
    source: str,
) -> None:
    conn.execute(
        """
        INSERT INTO recipe_items(recipe_id, item, grams, kcal_per_100g, kcal, protein_g, carbs_g, fat_g, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(recipe_id),
            item,
            float(grams),
            float(kcal_per_100g),
            float(kcal),
            None if protein_g is None else float(protein_g),
            None if carbs_g is None else float(carbs_g),
            None if fat_g is None else float(fat_g),
            source,
        ),
    )
    conn.commit()


def read_recipes(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM recipes ORDER BY name", conn)


def read_recipe_items(conn: sqlite3.Connection, recipe_id: int) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM recipe_items WHERE recipe_id = ? ORDER BY id",
        conn,
        params=(int(recipe_id),),
    )


def delete_recipe_item(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM recipe_items WHERE id = ?", (int(item_id),))
    conn.commit()


def delete_recipe(conn: sqlite3.Connection, recipe_id: int) -> None:
    conn.execute("DELETE FROM recipe_items WHERE recipe_id = ?", (int(recipe_id),))
    conn.execute("DELETE FROM recipes WHERE id = ?", (int(recipe_id),))
    conn.commit()


# ----------------------------
# Saved meals
# ----------------------------

def create_saved_meal(conn: sqlite3.Connection, name: str, meal: str) -> int:
    cur = conn.execute(
        "INSERT INTO saved_meals(name, meal, created_at) VALUES (?, ?, ?)",
        (name, meal, datetime.utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()
    return int(cur.lastrowid)


def add_saved_meal_item(
    conn: sqlite3.Connection,
    saved_meal_id: int,
    item: str,
    grams: float,
    kcal_per_100g: float,
    kcal: float,
    protein_g: Optional[float],
    carbs_g: Optional[float],
    fat_g: Optional[float],
    source: str,
) -> None:
    conn.execute(
        """
        INSERT INTO saved_meal_items(saved_meal_id, item, grams, kcal_per_100g, kcal, protein_g, carbs_g, fat_g, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(saved_meal_id),
            item,
            float(grams),
            float(kcal_per_100g),
            float(kcal),
            None if protein_g is None else float(protein_g),
            None if carbs_g is None else float(carbs_g),
            None if fat_g is None else float(fat_g),
            source,
        ),
    )
    conn.commit()


def read_saved_meals(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM saved_meals ORDER BY name", conn)


def read_saved_meal_items(conn: sqlite3.Connection, saved_meal_id: int) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM saved_meal_items WHERE saved_meal_id = ? ORDER BY id",
        conn,
        params=(int(saved_meal_id),),
    )


def delete_saved_meal(conn: sqlite3.Connection, saved_meal_id: int) -> None:
    conn.execute("DELETE FROM saved_meal_items WHERE saved_meal_id = ?", (int(saved_meal_id),))
    conn.execute("DELETE FROM saved_meals WHERE id = ?", (int(saved_meal_id),))
    conn.commit()


# ----------------------------
# Barcode lookup (Open Food Facts)
# ----------------------------

def get_cached_barcode(conn: sqlite3.Connection, barcode: str) -> Optional[dict]:
    cur = conn.execute(
        "SELECT * FROM barcode_cache WHERE barcode = ?",
        (barcode,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return dict(row)


def upsert_barcode_cache(
    conn: sqlite3.Connection,
    barcode: str,
    product_name: str,
    kcal_per_100g: Optional[float],
    p_100g: Optional[float],
    c_100g: Optional[float],
    f_100g: Optional[float],
    raw_json: dict,
) -> None:
    conn.execute(
        """
        INSERT INTO barcode_cache(barcode, product_name, kcal_per_100g, protein_g_100g, carbs_g_100g, fat_g_100g, raw_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(barcode) DO UPDATE SET
            product_name=excluded.product_name,
            kcal_per_100g=excluded.kcal_per_100g,
            protein_g_100g=excluded.protein_g_100g,
            carbs_g_100g=excluded.carbs_g_100g,
            fat_g_100g=excluded.fat_g_100g,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """,
        (
            barcode,
            product_name,
            None if kcal_per_100g is None else float(kcal_per_100g),
            None if p_100g is None else float(p_100g),
            None if c_100g is None else float(c_100g),
            None if f_100g is None else float(f_100g),
            json.dumps(raw_json),
            datetime.utcnow().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def fetch_off_product(barcode: str) -> Tuple[Optional[str], Optional[float], Optional[float], Optional[float], Optional[float], dict]:
    url = OFF_PRODUCT_URL.format(barcode=barcode)
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()

    # OFF v2 responses: { status: 1/0, product: {...} }
    product = data.get("product") or {}
    name = product.get("product_name") or product.get("generic_name") or ""

    nutr = product.get("nutriments") or {}

    # Prefer explicit kcal
    kcal_100g = nutr.get("energy-kcal_100g")
    if kcal_100g is None:
        # Sometimes only energy-kcal_value etc.
        kcal_100g = nutr.get("energy-kcal")

    p_100g = nutr.get("proteins_100g")
    c_100g = nutr.get("carbohydrates_100g")
    f_100g = nutr.get("fat_100g")

    return name, safe_float(kcal_100g, 0.0) if kcal_100g is not None else None, (
        safe_float(p_100g, 0.0) if p_100g is not None else None
    ), (
        safe_float(c_100g, 0.0) if c_100g is not None else None
    ), (
        safe_float(f_100g, 0.0) if f_100g is not None else None
    ), data


# Optional barcode decode from image

def try_decode_barcode_from_image(uploaded_file) -> Optional[str]:
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode

        img = Image.open(uploaded_file)
        codes = decode(img)
        if not codes:
            return None
        # Return first
        return codes[0].data.decode("utf-8")
    except Exception:
        return None


# ----------------------------
# Search helpers
# ----------------------------

def search_foods(
    ingredients: pd.DataFrame,
    query: str,
    main_category: str,
    cuisine_tag: str,
    shelf_stable_only: bool,
) -> pd.DataFrame:
    df = ingredients

    if main_category and main_category != "All":
        df = df[df["MainCategory"] == main_category]

    if cuisine_tag and cuisine_tag != "All":
        df = df[df["CuisineTags"].str.contains(cuisine_tag, case=False, na=False)]

    if shelf_stable_only:
        df = df[df["ShelfStable"].astype(str).str.lower().eq("yes")]

    q = (query or "").strip().lower()
    if q:
        df = df[df["Item"].str.lower().str.contains(q, na=False)]

    cols = [
        "Item",
        "MainCategory",
        "Subcategory",
        "CuisineTags",
        "Energy (kcal)",
        "Basis",
        "ShelfStable",
    ]
    for extra in ["Protein_g_100g", "Carbs_g_100g", "Fat_g_100g"]:
        if extra in df.columns:
            cols.append(extra)

    return df[cols].sort_values(["MainCategory", "Subcategory", "Item"]).reset_index(drop=True)


def build_lookup(ingredients: pd.DataFrame) -> dict:
    return {
        row["Item"].strip(): row
        for _, row in ingredients.iterrows()
        if isinstance(row.get("Item"), str)
    }


def portion_options_for_item(portions: pd.DataFrame, item: str) -> pd.DataFrame:
    key = item.strip().lower()
    p = portions[portions["_key"] == key].copy()
    if p.empty:
        return p
    if "Energy (kcal) per portion/item" in p.columns:
        p = p.sort_values(
            by=["Energy (kcal) per portion/item"],
            ascending=False,
            na_position="last",
        )
    return p


# ----------------------------
# Streamlit UI
# ----------------------------
st.set_page_config(page_title="UK Calorie Tracker", page_icon="🥗", layout="wide")

# ----------------------------
# Optional: simple passcode gate (for a personal app)
# Add to Streamlit Secrets:
# [app]
# passcode = "choose-a-secret"
# ----------------------------
app_pass = None
try:
    app_pass = st.secrets.get("app", {}).get("passcode")
except Exception:
    app_pass = None

if app_pass:
    if "authed" not in st.session_state:
        st.session_state["authed"] = False

    if not st.session_state["authed"]:
        st.title("🔒 UK Calorie Tracker")
        st.write("Enter your passcode to continue.")
        entered = st.text_input("Passcode", type="password")
        if st.button("Unlock"):
            if entered == app_pass:
                st.session_state["authed"] = True
                st.rerun()
            else:
                st.error("Incorrect passcode.")
        st.stop()

st.title("🥗 UK Calorie Tracker")
st.caption("Lose It!-style daily calorie diary using your UK food database + recipes, saved meals, barcodes, macros, weight trends.")

# Sidebar: settings
with st.sidebar:
    st.header("Settings")
    xlsx_path = st.text_input("Food database (xlsx)", value=DEFAULT_DB_PATH)
    daily_budget = st.number_input("Daily calorie budget", min_value=0, max_value=20000, value=2000, step=50)

    st.subheader("Macro targets (daily)")
    protein_target = st.number_input("Protein target (g)", min_value=0, max_value=1000, value=120, step=5)
    carbs_target = st.number_input("Carbs target (g)", min_value=0, max_value=2000, value=250, step=10)
    fat_target = st.number_input("Fat target (g)", min_value=0, max_value=1000, value=70, step=5)

    show_kj = st.toggle("Also show kJ (approx)", value=False)
    st.divider()
    st.caption("Diary / recipes / saved meals stored locally:")
    st.code(SQLITE_PATH)

# Load data
try:
    ingredients_df, portions_df = load_food_database(xlsx_path)
except Exception as e:
    st.error(
        "Couldn't load the Excel database. Check the path and sheet names."
        f"Error: {e}"
    )
    st.stop()

# Filter options
main_categories = ["All"] + sorted([c for c in ingredients_df["MainCategory"].dropna().unique() if str(c).strip()])
all_tags = set()
for t in ingredients_df["CuisineTags"].fillna(""):
    for token in str(t).split(","):
        token = token.strip()
        if token:
            all_tags.add(token)
cuisine_tags = ["All"] + sorted(all_tags)

conn = get_conn()

# Favourites
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS favourites (
        item_key TEXT PRIMARY KEY,
        item TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """
)
conn.commit()

lookup = build_lookup(ingredients_df)

# Tabs
(tab_diary, tab_add, tab_recipes, tab_saved, tab_barcode, tab_weight, tab_trends) = st.tabs(
    ["📅 Diary", "➕ Add food", "🍲 Recipes", "⭐ Saved meals", "🏷️ Barcode", "⚖️ Weight", "📈 Trends"]
)


# ----------------------------
# DIARY TAB
# ----------------------------
with tab_diary:
    col1, col2 = st.columns([1, 2])
    with col1:
        diary_date = st.date_input("Date", value=date.today())
        meal_filter = st.selectbox("Meal", options=["All"] + MEALS, index=0)

        st.divider()
        st.subheader("Quick actions")
        # Copy yesterday
        yday = diary_date - timedelta(days=1)
        if st.button(f"Copy yesterday → {diary_date.isoformat()}"):
            y_entries = read_entries(conn, yday)
            if y_entries.empty:
                st.warning(f"No entries on {yday.isoformat()}.")
            else:
                for _, e in y_entries.iterrows():
                    add_diary_entry(
                        conn,
                        entry_date=diary_date,
                        meal=str(e["meal"]),
                        item=str(e["item"]),
                        grams=float(e["grams"]),
                        kcal_per_100g=float(e["kcal_per_100g"]),
                        kcal=float(e["kcal"]),
                        protein_g=None if pd.isna(e.get("protein_g")) else float(e.get("protein_g")),
                        carbs_g=None if pd.isna(e.get("carbs_g")) else float(e.get("carbs_g")),
                        fat_g=None if pd.isna(e.get("fat_g")) else float(e.get("fat_g")),
                        source=(str(e.get("source") or "") + " | copied").strip(" |"),
                    )
                st.success(f"Copied {len(y_entries)} entries from {yday.isoformat()}.")
                st.rerun()

        # Quick add calories
        with st.expander("Quick-add calories"):
            qa_meal = st.selectbox("Meal", options=MEALS, index=3, key="qa_meal")
            qa_kcal = st.number_input("Calories to add", min_value=0.0, max_value=5000.0, value=100.0, step=10.0, key="qa_kcal")
            qa_note = st.text_input("Note (optional)", placeholder="e.g., Coffee + biscuit", key="qa_note")
            if st.button("Add quick calories", key="qa_btn"):
                add_diary_entry(
                    conn,
                    entry_date=diary_date,
                    meal=qa_meal,
                    item=f"Quick add: {qa_note}" if qa_note.strip() else "Quick add",
                    grams=0.0,
                    kcal_per_100g=0.0,
                    kcal=float(qa_kcal),
                    protein_g=None,
                    carbs_g=None,
                    fat_g=None,
                    source="quick-add",
                )
                st.success("Added.")
                st.rerun()

    entries = read_entries(conn, diary_date)
    burn = read_burn_entries(conn, diary_date)
    if meal_filter != "All" and not entries.empty:
        entries = entries[entries["meal"] == meal_filter]

    total_kcal = float(entries["kcal"].sum()) if not entries.empty else 0.0
    burned_kcal = float(burn["kcal_burned"].sum()) if not burn.empty else 0.0

    # Net calories = consumed - burned
    net_kcal = total_kcal - burned_kcal
    # Remaining vs budget uses net (so exercise increases remaining)
    remaining = float(daily_budget) - net_kcal

    # macros
    total_p = float(entries["protein_g"].fillna(0).sum()) if (not entries.empty and "protein_g" in entries.columns) else 0.0
    total_c = float(entries["carbs_g"].fillna(0).sum()) if (not entries.empty and "carbs_g" in entries.columns) else 0.0
    total_f = float(entries["fat_g"].fillna(0).sum()) if (not entries.empty and "fat_g" in entries.columns) else 0.0

    with col2:
        mcols = st.columns(4)
        mcols[0].metric("Consumed", f"{total_kcal:.0f} kcal")
        mcols[1].metric("Burned", f"{burned_kcal:.0f} kcal")
        mcols[2].metric("Net", f"{net_kcal:.0f} kcal")
        mcols[3].metric("Remaining", f"{remaining:.0f} kcal")
        if show_kj:
            st.caption(f"Approx: {total_kcal*4.184:.0f} kJ consumed")

        macro_cols = st.columns(3)
        macro_cols[0].metric("Protein", f"{total_p:.0f}g", delta=f"{total_p-protein_target:+.0f}g")
        macro_cols[1].metric("Carbs", f"{total_c:.0f}g", delta=f"{total_c-carbs_target:+.0f}g")
        macro_cols[2].metric("Fat", f"{total_f:.0f}g", delta=f"{total_f-fat_target:+.0f}g")

    st.subheader("Entries")

    # Log burned calories
    with st.expander("Log calories burned (exercise / activity / daily living)"):
        bc1, bc2, bc3 = st.columns([1, 2, 1])
        with bc1:
            bcat = st.selectbox("Category", options=["Exercise", "Activity", "Daily living"], key="burn_cat")
        with bc2:
            bname = st.text_input("Description", placeholder="e.g., 30 min run, brisk walk, active job", key="burn_name")
        with bc3:
            bkcal = st.number_input("kcal burned", min_value=0.0, max_value=5000.0, value=200.0, step=10.0, key="burn_kcal")

        if st.button("Add burned calories", key="burn_add"):
            add_burn_entry(conn, diary_date, bcat, bname.strip() or bcat, float(bkcal))
            st.success("Added.")
            st.rerun()

    if not burn.empty:
        st.write("**Burn entries**")
        bdisp = burn.rename(columns={"category": "Category", "name": "Description", "kcal_burned": "kcal"})
        st.dataframe(bdisp[["id", "Category", "Description", "kcal"]], hide_index=True, use_container_width=True)
        with st.expander("Delete a burn entry"):
            bid = st.selectbox("Burn entry id", options=bdisp["id"].tolist(), key="burn_del_id")
            if st.button("Delete burn entry", key="burn_del_btn"):
                delete_burn_entry(conn, int(bid))
                st.success("Deleted.")
                st.rerun()
    if entries.empty:
        st.info("No entries yet. Use the other tabs to log foods, recipes, saved meals, or barcodes.")
    else:
        # Meal totals
        meal_totals = entries.groupby("meal")["kcal"].sum().reindex(MEALS).dropna()
        if not meal_totals.empty:
            st.write("**Per meal totals**")
            st.dataframe(
                meal_totals.reset_index().rename(columns={"meal": "Meal", "kcal": "kcal"}),
                hide_index=True,
                use_container_width=True,
            )

        display = entries.rename(
            columns={
                "meal": "Meal",
                "item": "Item",
                "grams": "Grams",
                "kcal_per_100g": "kcal/100g",
                "kcal": "kcal",
                "protein_g": "Protein (g)",
                "carbs_g": "Carbs (g)",
                "fat_g": "Fat (g)",
            }
        )

        cols = ["id", "Meal", "Item", "Grams", "kcal/100g", "kcal", "Protein (g)", "Carbs (g)", "Fat (g)", "source"]
        st.dataframe(display[cols], hide_index=True, use_container_width=True)

        with st.expander("Delete an entry"):
            entry_ids = display["id"].tolist()
            to_delete = st.selectbox("Select entry id", options=entry_ids)
            if st.button("Delete", type="secondary"):
                delete_diary_entry(conn, int(to_delete))
                st.success("Deleted.")
                st.rerun()


# ----------------------------
# ADD FOOD TAB
# ----------------------------
with tab_add:
    st.subheader("Search & log")

    # Ensure a consistent selected item across reruns
    if "selected_item" not in st.session_state:
        st.session_state["selected_item"] = ""

    # Favourites & Recents (clickable)
    # Streamlit 1.52 supports dataframe row selection via on_select.
    fav_col, rec_col = st.columns(2)

    with fav_col:
        st.write("**Favourite foods**")
        favs = pd.read_sql_query("SELECT * FROM favourites ORDER BY created_at DESC", conn)
        if favs.empty:
            st.caption("No favourites yet. Select a food below, then favourite it.")
            picked_fav = None
        else:
            fav_view = favs[["item", "created_at"]].rename(columns={"item": "Favourite", "created_at": "Added"})
            fav_event = st.dataframe(
                fav_view,
                hide_index=True,
                use_container_width=True,
                selection_mode="single-row",
                on_select="rerun",
                key="fav_table",
            )
            sel = getattr(fav_event, "selection", {}) or {}
            rows = sel.get("rows", []) or []
            picked_fav = None
            if rows:
                picked_fav = str(favs.iloc[int(rows[0])]["item"])
                st.caption(f"Selected: {picked_fav}")

            bcols = st.columns(2)
            if bcols[0].button("Use selected", key="use_fav") and picked_fav:
                st.session_state["selected_item"] = picked_fav
                st.rerun()
            if bcols[1].button("Remove selected", key="rm_fav") and picked_fav:
                conn.execute("DELETE FROM favourites WHERE item_key = ?", (picked_fav.strip().lower(),))
                conn.commit()
                if st.session_state.get("selected_item") == picked_fav:
                    st.session_state["selected_item"] = ""
                st.rerun()

    with rec_col:
        st.write("**Recent foods**")
        recent = pd.read_sql_query(
            """
            SELECT item, MAX(created_at) AS last_used
            FROM diary_entries
            WHERE item NOT LIKE 'Recipe:%' AND item NOT LIKE 'Barcode:%' AND item NOT LIKE 'Quick add%'
            GROUP BY item
            ORDER BY last_used DESC
            LIMIT 25
            """,
            conn,
        )
        if recent.empty:
            st.caption("No recent foods yet.")
            picked_rec = None
        else:
            rec_view = recent.rename(columns={"item": "Recent", "last_used": "Last used"})
            rec_event = st.dataframe(
                rec_view,
                hide_index=True,
                use_container_width=True,
                selection_mode="single-row",
                on_select="rerun",
                key="rec_table",
            )
            sel = getattr(rec_event, "selection", {}) or {}
            rows = sel.get("rows", []) or []
            picked_rec = None
            if rows:
                picked_rec = str(recent.iloc[int(rows[0])]["item"])
                st.caption(f"Selected: {picked_rec}")
            if st.button("Use selected", key="use_rec") and picked_rec:
                st.session_state["selected_item"] = picked_rec
                st.rerun()

    st.divider()

    left, right = st.columns([2, 1])
    with left:
        q = st.text_input("Search foods", placeholder="e.g., chicken breast, basmati rice, olive oil")
    with right:
        chosen_main = st.selectbox("Category", options=main_categories)
        chosen_tag = st.selectbox("Cuisine tag", options=cuisine_tags)
        shelf_only = st.toggle("Shelf-stable only", value=False)

    results = search_foods(ingredients_df, q, chosen_main, chosen_tag, shelf_only)
    st.caption(f"Showing {len(results)} matching foods")
    st.dataframe(results, use_container_width=True, hide_index=True)

    st.divider()

    st.subheader("Log selected food")
    options = results["Item"].tolist()[:5000]

    # Default the selectbox to the current session selection if possible
    current = st.session_state.get("selected_item", "")
    default_index = options.index(current) if (current in options) else 0

    selected_item = st.selectbox(
        "Food",
        options=options if options else [""],
        index=default_index,
        key="food_select",
    )

    # Keep session selection in sync
    if selected_item:
        st.session_state["selected_item"] = selected_item

    # Favourite toggle button for currently selected item
    if selected_item:
        fav_key = selected_item.strip().lower()
        is_fav = conn.execute("SELECT 1 FROM favourites WHERE item_key = ?", (fav_key,)).fetchone() is not None
        fav_label = "⭐ Unfavourite" if is_fav else "☆ Favourite"
        if st.button(fav_label, key="fav_toggle"):
            if is_fav:
                conn.execute("DELETE FROM favourites WHERE item_key = ?", (fav_key,))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO favourites(item_key, item, created_at) VALUES (?, ?, ?)",
                    (fav_key, selected_item, datetime.utcnow().isoformat(timespec="seconds")),
                )
            conn.commit()
            st.rerun()

    if selected_item and selected_item in lookup:
        r = lookup[selected_item]
        kcal100 = safe_float(r.get("Energy (kcal)"), 0.0)
        item_key = str(r.get("_key") or selected_item.strip().lower())

        # Macros (from sheet if present) + override
        protein100 = r.get("Protein_g_100g") if "Protein_g_100g" in r.index else None
        carbs100 = r.get("Carbs_g_100g") if "Carbs_g_100g" in r.index else None
        fat100 = r.get("Fat_g_100g") if "Fat_g_100g" in r.index else None

        override = get_macro_override(conn, item_key)
        if override:
            protein100 = override.get("Protein_g_100g")
            carbs100 = override.get("Carbs_g_100g")
            fat100 = override.get("Fat_g_100g")

        with st.expander("Macro override for this item (optional)"):
            st.caption("Only needed if you want macro tracking for foods without macros in the database.")
            op, oc, of = st.columns(3)
            p_in = op.number_input("Protein g/100g", min_value=0.0, max_value=100.0, value=safe_float(protein100, 0.0), step=0.5)
            c_in = oc.number_input("Carbs g/100g", min_value=0.0, max_value=200.0, value=safe_float(carbs100, 0.0), step=0.5)
            f_in = of.number_input("Fat g/100g", min_value=0.0, max_value=100.0, value=safe_float(fat100, 0.0), step=0.5)
            if st.button("Save macro override"):
                upsert_macro_override(conn, item_key, p_in, c_in, f_in)
                st.success("Saved macro override.")
                st.rerun()

        c1, c2, c3 = st.columns([1, 1, 1])
        with c1:
            entry_date = st.date_input("Log date", value=date.today(), key="log_date")
        with c2:
            meal = st.selectbox("Meal", options=MEALS, index=1)
        with c3:
            mode = st.radio("How much?", options=["Grams", "Typical portion"], horizontal=True)

        grams = None
        kcal = None
        source = ""
        if mode == "Grams":
            grams = st.number_input("Grams", min_value=0.0, max_value=5000.0, value=100.0, step=5.0)
            kcal = kcal_for_grams(kcal100, grams)
            source = "per 100g"
        else:
            p = portion_options_for_item(portions_df, selected_item)
            if p.empty:
                st.warning("No portion presets for this item yet. Use grams mode.")
                grams = st.number_input("Grams", min_value=0.0, max_value=5000.0, value=100.0, step=5.0)
                kcal = kcal_for_grams(kcal100, grams)
                source = "per 100g"
            else:
                p = p.copy()
                p["_label"] = p.apply(
                    lambda x: f"{x['Portion']} • {x['Portion (g/ml)']}g • {safe_float(x.get('Energy (kcal) per portion/item'), 0.0):.0f} kcal",
                    axis=1,
                )
                choice = st.selectbox("Portion", options=p["_label"].tolist())
                chosen = p[p["_label"] == choice].iloc[0]

                # Allow multiples of the selected portion (e.g., 2 slices of bread)
                mult = st.number_input(
                    "How many portions?",
                    min_value=1.0,
                    max_value=50.0,
                    value=1.0,
                    step=1.0,
                    help="Use this to log multiple identical portions at once (e.g., 2 slices, 3 biscuits).",
                    key="portion_multiplier",
                )

                grams_one = safe_float(chosen.get("Portion (g/ml)"), 0.0)
                kcal_one = chosen.get("Energy (kcal) per portion/item")
                kcal_one = safe_float(kcal_one, 0.0) if kcal_one is not None else kcal_for_grams(kcal100, grams_one)

                grams = grams_one * float(mult)
                kcal = kcal_one * float(mult)
                source = f"portion: {chosen['Portion']} x{float(mult):g}"

        p_g, c_g, f_g = macros_for_grams(
            None if protein100 is None else safe_float(protein100, 0.0),
            None if carbs100 is None else safe_float(carbs100, 0.0),
            None if fat100 is None else safe_float(fat100, 0.0),
            grams,
        )

        st.write("**Preview**")
        prev = st.columns(5)
        prev[0].metric("Food", selected_item)
        prev[1].metric("Amount", f"{grams:.0f} g")
        prev[2].metric("Calories", f"{kcal:.0f} kcal")
        prev[3].metric("Protein", f"{(p_g or 0):.0f} g")
        prev[4].metric("Carbs/Fat", f"{(c_g or 0):.0f}g / {(f_g or 0):.0f}g")

        if st.button("Log to diary", type="primary"):
            add_diary_entry(
                conn,
                entry_date=entry_date,
                meal=meal,
                item=selected_item,
                grams=float(grams),
                kcal_per_100g=float(kcal100),
                kcal=float(kcal),
                protein_g=p_g,
                carbs_g=c_g,
                fat_g=f_g,
                source=source,
            )
            st.success("Logged!")
            st.rerun()


# ----------------------------
# RECIPES TAB
# ----------------------------
with tab_recipes:
    st.subheader("Custom recipes")

    rc1, rc2 = st.columns([1, 1])
    with rc1:
        st.write("**Create a recipe**")
        new_name = st.text_input("Recipe name", key="recipe_name")
        new_servings = st.number_input("Servings", min_value=1.0, max_value=100.0, value=4.0, step=1.0)
        new_notes = st.text_area("Notes (optional)", key="recipe_notes")
        if st.button("Create recipe"):
            if not new_name.strip():
                st.warning("Please enter a recipe name.")
            else:
                rid = create_recipe(conn, new_name.strip(), float(new_servings), new_notes.strip())
                st.success(f"Created recipe: {new_name} (id {rid})")
                st.rerun()

    recipes_df = read_recipes(conn)
    if recipes_df.empty:
        st.info("No recipes yet. Create one on the left.")
    else:
        with rc2:
            recipe_label_map = {f"{r['name']} (servings: {r['servings']})": int(r["id"]) for _, r in recipes_df.iterrows()}
            selected_recipe_label = st.selectbox("Select recipe", options=list(recipe_label_map.keys()))
            selected_recipe_id = recipe_label_map[selected_recipe_label]

            recipe_row = recipes_df[recipes_df["id"] == selected_recipe_id].iloc[0]
            servings = safe_float(recipe_row["servings"], 1.0)

            items_df = read_recipe_items(conn, selected_recipe_id)
            total_kcal = float(items_df["kcal"].sum()) if not items_df.empty else 0.0
            total_p = float(items_df["protein_g"].fillna(0).sum()) if not items_df.empty else 0.0
            total_c = float(items_df["carbs_g"].fillna(0).sum()) if not items_df.empty else 0.0
            total_f = float(items_df["fat_g"].fillna(0).sum()) if not items_df.empty else 0.0

            st.write("**Recipe totals**")
            m = st.columns(4)
            m[0].metric("Total kcal", f"{total_kcal:.0f}")
            m[1].metric("Per serving kcal", f"{(total_kcal/servings):.0f}")
            m[2].metric("Per serving protein", f"{(total_p/servings):.0f} g")
            m[3].metric("Per serving carbs/fat", f"{(total_c/servings):.0f}g / {(total_f/servings):.0f}g")

            st.write("**Add ingredient to recipe**")
            # Use Add-food style search
            rq = st.text_input("Search foods", key="recipe_food_search")
            rres = search_foods(ingredients_df, rq, "All", "All", False)
            ropts = rres["Item"].tolist()[:2000]
            ritem = st.selectbox("Ingredient", options=ropts if ropts else [""], key="recipe_item")
            rgrams = st.number_input("Grams", min_value=0.0, max_value=50000.0, value=100.0, step=5.0, key="recipe_grams")

            if ritem and ritem in lookup:
                rr = lookup[ritem]
                kcal100 = safe_float(rr.get("Energy (kcal)"), 0.0)
                item_key = str(rr.get("_key") or ritem.strip().lower())

                protein100 = rr.get("Protein_g_100g") if "Protein_g_100g" in rr.index else None
                carbs100 = rr.get("Carbs_g_100g") if "Carbs_g_100g" in rr.index else None
                fat100 = rr.get("Fat_g_100g") if "Fat_g_100g" in rr.index else None
                override = get_macro_override(conn, item_key)
                if override:
                    protein100 = override.get("Protein_g_100g")
                    carbs100 = override.get("Carbs_g_100g")
                    fat100 = override.get("Fat_g_100g")

                kcal = kcal_for_grams(kcal100, rgrams)
                p_g, c_g, f_g = macros_for_grams(
                    None if protein100 is None else safe_float(protein100, 0.0),
                    None if carbs100 is None else safe_float(carbs100, 0.0),
                    None if fat100 is None else safe_float(fat100, 0.0),
                    rgrams,
                )

                if st.button("Add to recipe"):
                    add_recipe_item(
                        conn,
                        selected_recipe_id,
                        ritem,
                        float(rgrams),
                        float(kcal100),
                        float(kcal),
                        p_g,
                        c_g,
                        f_g,
                        source="recipe ingredient",
                    )
                    st.success("Added.")
                    st.rerun()

            st.write("**Recipe ingredients**")
            if items_df.empty:
                st.info("No ingredients yet.")
            else:
                st.dataframe(
                    items_df[["id", "item", "grams", "kcal", "protein_g", "carbs_g", "fat_g"]].rename(
                        columns={
                            "item": "Item",
                            "grams": "Grams",
                            "kcal": "kcal",
                            "protein_g": "Protein (g)",
                            "carbs_g": "Carbs (g)",
                            "fat_g": "Fat (g)",
                        }
                    ),
                    hide_index=True,
                    use_container_width=True,
                )
                with st.expander("Delete ingredient"):
                    rid_list = items_df["id"].tolist()
                    del_id = st.selectbox("Ingredient id", options=rid_list, key="del_recipe_item")
                    if st.button("Delete ingredient", key="btn_del_recipe_item"):
                        delete_recipe_item(conn, int(del_id))
                        st.success("Deleted.")
                        st.rerun()

            st.divider()
            st.write("**Log this recipe**")
            ld1, ld2, ld3 = st.columns(3)
            with ld1:
                log_date = st.date_input("Log date", value=date.today(), key="log_recipe_date")
            with ld2:
                log_meal = st.selectbox("Meal", options=MEALS, index=2, key="log_recipe_meal")
            with ld3:
                servings_eaten = st.number_input("Servings eaten", min_value=0.1, max_value=50.0, value=1.0, step=0.5)

            if st.button("Log recipe to diary", type="primary", key="btn_log_recipe"):
                if servings <= 0:
                    st.warning("Recipe servings must be > 0.")
                else:
                    kcal = total_kcal * float(servings_eaten) / servings
                    p = total_p * float(servings_eaten) / servings
                    c = total_c * float(servings_eaten) / servings
                    f = total_f * float(servings_eaten) / servings
                    add_diary_entry(
                        conn,
                        log_date,
                        log_meal,
                        item=f"Recipe: {recipe_row['name']}",
                        grams=0.0,
                        kcal_per_100g=0.0,
                        kcal=float(kcal),
                        protein_g=float(p) if p is not None else None,
                        carbs_g=float(c) if c is not None else None,
                        fat_g=float(f) if f is not None else None,
                        source="recipe",
                    )
                    st.success("Logged recipe!")
                    st.rerun()

            with st.expander("Delete recipe"):
                if st.button("Delete this recipe", type="secondary"):
                    delete_recipe(conn, selected_recipe_id)
                    st.success("Deleted recipe.")
                    st.rerun()


# ----------------------------
# SAVED MEALS TAB
# ----------------------------
with tab_saved:
    st.subheader("Saved meals (templates)")
    st.caption("Save a frequently repeated meal and re-log it in one click.")

    sm1, sm2 = st.columns([1, 2])
    with sm1:
        st.write("**Create saved meal**")
        sm_name = st.text_input("Name", key="sm_name")
        sm_meal = st.selectbox("Default meal", options=MEALS, index=1, key="sm_meal")
        if st.button("Create saved meal"):
            if not sm_name.strip():
                st.warning("Please enter a name.")
            else:
                sm_id = create_saved_meal(conn, sm_name.strip(), sm_meal)
                st.success(f"Created saved meal: {sm_name} (id {sm_id})")
                st.rerun()

    saved_df = read_saved_meals(conn)
    if saved_df.empty:
        st.info("No saved meals yet.")
    else:
        with sm2:
            sm_map = {f"{r['name']} ({r['meal']})": int(r["id"]) for _, r in saved_df.iterrows()}
            chosen_label = st.selectbox("Select saved meal", options=list(sm_map.keys()))
            chosen_id = sm_map[chosen_label]

            items = read_saved_meal_items(conn, chosen_id)
            st.write("**Items in saved meal**")
            if items.empty:
                st.info("Add items below.")
            else:
                st.dataframe(items[["item", "grams", "kcal", "protein_g", "carbs_g", "fat_g"]], hide_index=True, use_container_width=True)

            st.write("**Add item to saved meal**")
            sm_q = st.text_input("Search foods", key="sm_food_search")
            sm_res = search_foods(ingredients_df, sm_q, "All", "All", False)
            sm_opts = sm_res["Item"].tolist()[:2000]
            sm_item = st.selectbox("Food", options=sm_opts if sm_opts else [""], key="sm_item")
            sm_grams = st.number_input("Grams", min_value=0.0, max_value=50000.0, value=100.0, step=5.0, key="sm_grams")

            if sm_item and sm_item in lookup:
                rr = lookup[sm_item]
                kcal100 = safe_float(rr.get("Energy (kcal)"), 0.0)
                item_key = str(rr.get("_key") or sm_item.strip().lower())
                protein100 = rr.get("Protein_g_100g") if "Protein_g_100g" in rr.index else None
                carbs100 = rr.get("Carbs_g_100g") if "Carbs_g_100g" in rr.index else None
                fat100 = rr.get("Fat_g_100g") if "Fat_g_100g" in rr.index else None
                override = get_macro_override(conn, item_key)
                if override:
                    protein100 = override.get("Protein_g_100g")
                    carbs100 = override.get("Carbs_g_100g")
                    fat100 = override.get("Fat_g_100g")

                kcal = kcal_for_grams(kcal100, sm_grams)
                p_g, c_g, f_g = macros_for_grams(
                    None if protein100 is None else safe_float(protein100, 0.0),
                    None if carbs100 is None else safe_float(carbs100, 0.0),
                    None if fat100 is None else safe_float(fat100, 0.0),
                    sm_grams,
                )

                if st.button("Add item", key="btn_add_sm"):
                    add_saved_meal_item(
                        conn,
                        chosen_id,
                        sm_item,
                        float(sm_grams),
                        float(kcal100),
                        float(kcal),
                        p_g,
                        c_g,
                        f_g,
                        source="saved meal",
                    )
                    st.success("Added.")
                    st.rerun()

            st.divider()
            st.write("**Log saved meal**")
            l1, l2, l3 = st.columns(3)
            with l1:
                log_date = st.date_input("Log date", value=date.today(), key="log_sm_date")
            with l2:
                default_meal = saved_df[saved_df["id"] == chosen_id].iloc[0]["meal"]
                log_meal = st.selectbox("Meal", options=MEALS, index=MEALS.index(default_meal) if default_meal in MEALS else 1, key="log_sm_meal")
            with l3:
                scale = st.number_input("Scale (e.g., 1.0 = as saved)", min_value=0.1, max_value=10.0, value=1.0, step=0.1)

            if st.button("Log to diary", type="primary", key="btn_log_sm"):
                items = read_saved_meal_items(conn, chosen_id)
                if items.empty:
                    st.warning("Saved meal has no items.")
                else:
                    for _, it in items.iterrows():
                        add_diary_entry(
                            conn,
                            log_date,
                            log_meal,
                            item=str(it["item"]),
                            grams=float(it["grams"]) * float(scale),
                            kcal_per_100g=float(it["kcal_per_100g"]),
                            kcal=float(it["kcal"]) * float(scale),
                            protein_g=(None if pd.isna(it.get("protein_g")) else float(it.get("protein_g")) * float(scale)),
                            carbs_g=(None if pd.isna(it.get("carbs_g")) else float(it.get("carbs_g")) * float(scale)),
                            fat_g=(None if pd.isna(it.get("fat_g")) else float(it.get("fat_g")) * float(scale)),
                            source="saved meal",
                        )
                    st.success("Logged saved meal!")
                    st.rerun()

            with st.expander("Delete saved meal"):
                if st.button("Delete this saved meal", type="secondary", key="btn_del_sm"):
                    delete_saved_meal(conn, chosen_id)
                    st.success("Deleted.")
                    st.rerun()


# ----------------------------
# BARCODE TAB
# ----------------------------
with tab_barcode:
    st.subheader("Barcode lookup (packaged foods)")
    st.caption("Enter a barcode to fetch nutrition from Open Food Facts. Results are cached locally.")

    b1, b2 = st.columns([1, 1])
    with b1:
        barcode = st.text_input("Barcode (EAN/UPC)", placeholder="e.g., 5012345678900")
        uploaded = st.file_uploader("Optional: upload a photo of the barcode (requires pyzbar)", type=["png", "jpg", "jpeg"])
        if uploaded and not barcode:
            decoded = try_decode_barcode_from_image(uploaded)
            if decoded:
                barcode = decoded
                st.success(f"Decoded barcode: {barcode}")
            else:
                st.info("Couldn't decode from image. You can still type the barcode manually.")

    if barcode:
        cached = get_cached_barcode(conn, barcode)
        use_cache = st.toggle("Use cached result if available", value=True)

        if cached and use_cache:
            name = cached.get("product_name") or "(unknown)"
            kcal100 = cached.get("kcal_per_100g")
            p100 = cached.get("protein_g_100g")
            c100 = cached.get("carbs_g_100g")
            f100 = cached.get("fat_g_100g")
            st.success("Loaded from cache.")
        else:
            try:
                with st.spinner("Fetching from Open Food Facts..."):
                    name, kcal100, p100, c100, f100, raw = fetch_off_product(barcode)
                upsert_barcode_cache(conn, barcode, name, kcal100, p100, c100, f100, raw)
                st.success("Fetched & cached.")
            except Exception as e:
                st.error(f"Barcode lookup failed: {e}")
                st.stop()

        with b2:
            st.write("**Product**")
            st.write(name or "(no name)")
            st.write(f"kcal/100g: **{safe_float(kcal100, 0.0):.0f}**")
            st.write(f"Protein/100g: **{safe_float(p100, 0.0):.1f}g**")
            st.write(f"Carbs/100g: **{safe_float(c100, 0.0):.1f}g**")
            st.write(f"Fat/100g: **{safe_float(f100, 0.0):.1f}g**")

        st.divider()
        st.subheader("Log this product")
        lc1, lc2, lc3 = st.columns(3)
        with lc1:
            log_date = st.date_input("Log date", value=date.today(), key="bc_date")
        with lc2:
            meal = st.selectbox("Meal", options=MEALS, index=3, key="bc_meal")
        with lc3:
            grams = st.number_input("Grams eaten", min_value=0.0, max_value=5000.0, value=100.0, step=5.0, key="bc_grams")

        kcal = kcal_for_grams(safe_float(kcal100, 0.0), grams)
        p_g, c_g, f_g = macros_for_grams(p100, c100, f100, grams)

        st.write("**Preview**")
        pc = st.columns(4)
        pc[0].metric("Calories", f"{kcal:.0f} kcal")
        pc[1].metric("Protein", f"{(p_g or 0):.1f} g")
        pc[2].metric("Carbs", f"{(c_g or 0):.1f} g")
        pc[3].metric("Fat", f"{(f_g or 0):.1f} g")

        if st.button("Log barcode food", type="primary"):
            add_diary_entry(
                conn,
                log_date,
                meal,
                item=f"Barcode: {name or barcode}",
                grams=float(grams),
                kcal_per_100g=float(safe_float(kcal100, 0.0)),
                kcal=float(kcal),
                protein_g=p_g,
                carbs_g=c_g,
                fat_g=f_g,
                source=f"barcode:{barcode}",
            )
            st.success("Logged!")
            st.rerun()


# ----------------------------
# WEIGHT TAB
# ----------------------------
with tab_weight:
    st.subheader("Weight tracking")

    w1, w2 = st.columns([1, 2])
    with w1:
        w_date = st.date_input("Date", value=date.today(), key="w_date")
        w_val = st.number_input("Weight (kg)", min_value=0.0, max_value=500.0, value=75.0, step=0.1)
        if st.button("Save weight"):
            conn.execute(
                """
                INSERT INTO weights(entry_date, weight_kg, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(entry_date) DO UPDATE SET
                    weight_kg=excluded.weight_kg,
                    created_at=excluded.created_at
                """,
                (w_date.isoformat(), float(w_val), datetime.utcnow().isoformat(timespec="seconds")),
            )
            conn.commit()
            st.success("Saved.")
            st.rerun()

    weights_df = pd.read_sql_query("SELECT * FROM weights ORDER BY entry_date", conn)
    if weights_df.empty:
        with w2:
            st.info("No weight entries yet.")
    else:
        weights_df["entry_date"] = pd.to_datetime(weights_df["entry_date"])
        with w2:
            st.write("**Weight over time**")
            st.line_chart(weights_df.set_index("entry_date")["weight_kg"])


# ----------------------------
# TRENDS TAB
# ----------------------------
with tab_trends:
    st.subheader("Weekly trend charts")

    end = date.today()
    start = end - timedelta(days=90)
    r1, r2 = st.columns(2)
    with r1:
        start = st.date_input("From", value=start, key="trend_from")
    with r2:
        end = st.date_input("To", value=end, key="trend_to")

    hist = read_entries_range(conn, start, end)
    if hist.empty:
        st.info("No diary data in this range.")
    else:
        hist["entry_date"] = pd.to_datetime(hist["entry_date"])
        daily = hist.groupby("entry_date").agg(
            kcal=("kcal", "sum"),
            protein=("protein_g", "sum"),
            carbs=("carbs_g", "sum"),
            fat=("fat_g", "sum"),
        ).fillna(0)

        # 7-day rolling average
        roll = daily.rolling(7, min_periods=1).mean()

        # Include burned calories and net
        burn_hist = read_burn_entries_range(conn, start, end)
        if not burn_hist.empty:
            burn_hist["entry_date"] = pd.to_datetime(burn_hist["entry_date"])
            burned_daily = burn_hist.groupby("entry_date")["kcal_burned"].sum().rename("burned")
        else:
            burned_daily = pd.Series(dtype=float, name="burned")

        daily = daily.join(burned_daily, how="left").fillna({"burned": 0})
        daily["net"] = daily["kcal"] - daily["burned"]

        # 7-day rolling average
        roll = daily.rolling(7, min_periods=1).mean()

        st.write("**Calories (consumed, burned, net + 7-day average)**")
        st.line_chart(
            pd.DataFrame(
                {
                    "consumed": daily["kcal"],
                    "burned": daily["burned"],
                    "net": daily["net"],
                    "net_7d_avg": roll["net"],
                }
            )
        )

        st.write("**Macros (7-day average)**")
        st.line_chart(pd.DataFrame({
            "protein_7d_avg": roll["protein"],
            "carbs_7d_avg": roll["carbs"],
            "fat_7d_avg": roll["fat"],
        }))

        # Weekly totals
        weekly = daily.resample("W").sum()
        st.write("**Weekly totals**")
        st.dataframe(weekly.reset_index().rename(columns={"entry_date": "Week"}), hide_index=True, use_container_width=True)

        # Merge weight if present
        weights_df = pd.read_sql_query("SELECT * FROM weights ORDER BY entry_date", conn)
        if not weights_df.empty:
            weights_df["entry_date"] = pd.to_datetime(weights_df["entry_date"])
            st.write("**Weight (with 7-day average)**")
            w = weights_df.set_index("entry_date")["weight_kg"].asfreq("D").interpolate(limit_direction="both")
            w_roll = w.rolling(7, min_periods=1).mean()
            st.line_chart(pd.DataFrame({"weight": w, "weight_7d_avg": w_roll}))


# ----------------------------
# HISTORY EXPORT (kept simple)
# ----------------------------
st.divider()
with st.expander("Export diary (CSV)"):
    h1, h2 = st.columns(2)
    with h1:
        exp_start = st.date_input("Export from", value=date.today().replace(day=1), key="exp_from")
    with h2:
        exp_end = st.date_input("Export to", value=date.today(), key="exp_to")

    hist = read_entries_range(conn, exp_start, exp_end)
    if hist.empty:
        st.info("No entries in this range.")
    else:
        csv = hist.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download CSV",
            data=csv,
            file_name=f"calorie_diary_{exp_start.isoformat()}_to_{exp_end.isoformat()}.csv",
            mime="text/csv",
        )

with st.expander("Backup / restore (JSON — works on iPhone)"):
    st.info(
        "This backup format is designed to restore reliably from iPhone/iPad. "
        "It exports all your entries, recipes, meals, weights, favourites, and barcode cache."
    )

    # Backup
    try:
        backup_obj = make_json_backup(conn)
        backup_bytes = json.dumps(backup_obj, indent=2).encode("utf-8")
        st.download_button(
            "Download backup (JSON)",
            data=backup_bytes,
            file_name="uk_calorie_tracker_backup.json",
            mime="application/json",
        )
        st.caption("Tip: Save this file to iCloud Drive or OneDrive so it’s easy to restore later.")
    except Exception as e:
        st.error(f"Couldn't create JSON backup: {e}")

    st.divider()

    # Restore
    st.write("**Restore from JSON backup**")
    st.caption(
        "Upload a uk_calorie_tracker_backup.json file to restore your diary. "
        "This will overwrite the current data on this hosted app."
    )
    uploaded_json = st.file_uploader(
        "Upload uk_calorie_tracker_backup.json",
        type=["json"],
        key="restore_json",
    )
    confirm_json = st.checkbox("Yes, overwrite the current diary with this JSON backup", value=False, key="confirm_restore_json")
    if uploaded_json and confirm_json and st.button("Restore JSON backup now", type="primary", key="btn_restore_json"):
        try:
            data = json.loads(uploaded_json.getvalue().decode("utf-8"))
            restore_json_backup(conn, data)
            st.success("Restored JSON backup. Reloading…")
            st.rerun()
        except Exception as e:
            st.error(f"JSON restore failed: {e}")


with st.expander("Backup / restore your full diary (SQLite file)"):
    st.warning(
        "On Streamlit Community Cloud, local files like the SQLite diary can be lost if the app restarts. "
        "Use this backup regularly if you rely on the hosted version."
    )

    # Backup
    try:
        if os.path.exists(SQLITE_PATH):
            with open(SQLITE_PATH, "rb") as f:
                db_bytes = f.read()
            st.download_button(
                "Download full diary backup (.sqlite)",
                data=db_bytes,
                file_name="calorie_diary.sqlite",
                mime="application/x-sqlite3",
            )
        else:
            st.caption("No local diary file exists yet.")
    except Exception as e:
        st.error(f"Couldn't read SQLite file for backup: {e}")

    st.divider()

    # Restore
    st.write("**Restore from a backup**")
    st.caption(
        "Upload a previously downloaded calorie_diary.sqlite to restore your data. "
        "This overwrites the current diary on this deployment."
    )
    uploaded_db = st.file_uploader("Upload calorie_diary.sqlite", type=["sqlite", "db"], key="restore_sqlite")
    confirm = st.checkbox("Yes, overwrite the current diary with this backup", value=False)
    if uploaded_db and confirm and st.button("Restore backup now", type="primary"):
        try:
            try:
                conn.close()
            except Exception:
                pass

            with open(SQLITE_PATH, "wb") as f:
                f.write(uploaded_db.getbuffer())

            st.success("Restored backup. Reloading…")
            st.rerun()
        except Exception as e:
            st.error(f"Restore failed: {e}")
