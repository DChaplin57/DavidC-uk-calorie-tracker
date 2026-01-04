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
import re


# ----------------------------
# Configuration
# ----------------------------

# ----------------------------
# UI THEMING (banners, buttons, table headers)
# ----------------------------

BRIGHT_PALETTE = {
    "quick": "#8E44FF",     # vivid purple
    "entries": "#1D64FF",   # vivid blue
    "search": "#00B8D9",    # bright cyan
    "log": "#FF2D55",       # hot pink/red
    "recipes": "#FF9500",   # bright orange
    "saved": "#34C759",     # bright green
    "barcode": "#FFCC00",   # bright yellow
    "weight": "#5AC8FA",    # light blue
    "trends": "#AF52DE",    # purple
    "help": "#5856D6",      # indigo
}

def inject_global_css() -> None:
    """Global CSS for banners + Streamlit button types."""
    st.markdown(
        """
        <style>
          /* Section banners */
          .section-banner {
            padding: 0.65rem 0.9rem;
            border-radius: 0.7rem;
            color: white;
            font-weight: 800;
            letter-spacing: 0.2px;
            margin: 0.7rem 0 0.6rem 0;
            box-shadow: 0 1px 0 rgba(0,0,0,0.08);
          }

          /* Button styling (shape only; colours handled by cb()) */
          div.stButton > button,
          div.stDownloadButton > button,
          div.stFormSubmitButton > button {
            border-radius: 12px !important;
            font-weight: 700 !important;
            border: 1px solid rgba(0,0,0,0.10) !important;
          }
          div.stButton > button:hover,
          div.stDownloadButton > button:hover,
          div.stFormSubmitButton > button:hover {
            filter: brightness(0.96);
          }
/* Reduce excessive whitespace around dataframes */
          div[data-testid="stDataFrame"] { margin-top: 0.25rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )

def banner(title: str, kind: str) -> None:
    color = BRIGHT_PALETTE.get(kind, "#1D64FF")
    st.markdown(
        f'<div class="section-banner" style="background:{color}">{title}</div>',
        unsafe_allow_html=True,
    )


# ----------------------------
# Multi-colour buttons (stable, per-function)
# ----------------------------
try:
    from streamlit_extras.stylable_container import stylable_container  # type: ignore
    _STYLABLE_CONTAINER_ERR = ""
except Exception as _e:  # pragma: no cover
    stylable_container = None  # type: ignore
    _STYLABLE_CONTAINER_ERR = repr(_e)

BUTTON_ROLE_COLORS = {
    "log": "#34C759",      # green
    "add": "#1D64FF",      # vivid blue
    "edit": "#AF52DE",     # purple
    "delete": "#FF3B30",   # red
    "search": "#00B8D9",   # cyan
    "fav": "#FF9500",      # orange
    "info": "#5AC8FA",     # light blue
    "neutral": "#8E8E93",  # grey
    "create": "#3366FF",
}

def _infer_button_role(label: str) -> str:
    t = (label or "").strip().lower()
    if any(k in t for k in ["log", "save weight", "save/update", "save/update", "save/update apple move", "add burned"]):
        return "log"
    if any(k in t for k in ["remove", "delete", "clear", "unfavourite", "unfavorite"]):
        return "delete"
    if any(k in t for k in ["favourite", "favorite", "☆", "⭐"]):
        return "fav"
    if any(k in t for k in ["search", "filter", "scan", "barcode", "lookup"]):
        return "search"
    if any(k in t for k in ["reset", "cancel", "close"]):
        return "neutral"
    if any(k in t for k in ["create", "new", "add", "use selected", "copy"]):
        return "add"
    return "info"

# ----------------------------
# Multi-colour button engine (robust: styles ALL buttons incl. column.button)
# ----------------------------
from streamlit.delta_generator import DeltaGenerator

_DG_BUTTON_ORIG = None

def _ensure_button_patch():
    """Patch Streamlit's DeltaGenerator.button so EVERY button (including col.button) is styled.

    We scope CSS to a per-button wrapper container using its `st-key-...` class.
    """
    global _DG_BUTTON_ORIG
    if _DG_BUTTON_ORIG is not None:
        return  # already patched

    _DG_BUTTON_ORIG = DeltaGenerator.button

    def _patched_button(self: DeltaGenerator, label, *args, **kwargs):
        # Allow an optional `role=` kwarg (used by our cb wrapper)
        role = kwargs.pop("role", None)
        if role is None:
            role = _infer_button_role(str(label))

        # Keep caller key if provided; otherwise generate a unique (per-run) key.
        key = kwargs.get("key")
        if key is None:
            n = int(st.session_state.get("_cb_auto_i", 0)) + 1
            st.session_state["_cb_auto_i"] = n
            slug = re.sub(r"[^a-zA-Z0-9_]+", "_", (str(label) or "btn")).strip("_")[:40] or "btn"
            key = f"cb_auto_{n}_{slug}"
            kwargs["key"] = key

        color = BUTTON_ROLE_COLORS.get(role, BUTTON_ROLE_COLORS.get("info", "#5AC8FA"))

        wrap_key = f"btnwrap__{key}"
        wrap_class = f"st-key-{wrap_key}"

        # Build wrapper in the CURRENT context (self can be a column, container, etc.)
        # Use self.container so layout stays correct.
        self.markdown(
            f"""<style>
            /* Shape defaults (no colour here) */
            div.{wrap_class} div.stButton > button {{
                border-radius: 12px !important;
                font-weight: 700 !important;
                padding: 0.45rem 0.85rem !important;
                border: 1px solid rgba(255,255,255,0.12) !important;
            }}
            /* Colour for this button */
            div.{wrap_class} div.stButton > button {{
                background: {color} !important;
                color: white !important;
            }}
            div.{wrap_class} div.stButton > button:hover {{
                filter: brightness(0.95);
            }}
            </style>""",
            unsafe_allow_html=True,
        )
        wrapper = self.container(key=wrap_key)

        # Render the real Streamlit button inside the wrapper (call original to avoid recursion)
        return _DG_BUTTON_ORIG(wrapper, label, *args, **kwargs)

    DeltaGenerator.button = _patched_button

# Ensure patch is applied early
_ensure_button_patch()

def cb(label: str, *args, role: str | None = None, key: str | None = None, **kwargs) -> bool:
    """Coloured button helper.

    - Uses `stylable_container` when available to apply per-role colours.
    - Never forwards unknown kwargs (like `role`) to Streamlit's `st.button`.
    - If `key` is omitted, generates a stable-enough unique key for this run.
    """
    # Determine / generate a key (Streamlit needs this for stable widget identity).
    btn_key = key or kwargs.get("key")
    if not btn_key:
        # Auto key: label slug + monotonic counter (per run). This is fine for buttons.
        counter = st.session_state.get("_cb_autokey_counter", 0) + 1
        st.session_state["_cb_autokey_counter"] = counter
        slug = re.sub(r"[^a-zA-Z0-9_]+", "_", (label or "btn")).strip("_")[:40] or "btn"
        btn_key = f"cb_{slug}_{counter}"
    kwargs["key"] = btn_key

    # Consume `role` without passing it to Streamlit.
    btn_role = role or _infer_button_role(label)

    # Choose colour.
    color = BUTTON_ROLE_COLORS.get(btn_role, BUTTON_ROLE_COLORS.get("neutral", "#8E8E93"))

    # If stylable_container isn't available, fall back to normal Streamlit button.
    if stylable_container is None:
        return st.button(label, *args, **kwargs)

    # Apply scoped CSS inside a stylable container.
    with stylable_container(
        key=f"btnwrap__{btn_key}",
        css_styles=f"""
        div.stButton > button {{
            background: {color} !important;
            color: white !important;
            border: 1px solid rgba(255,255,255,0.12) !important;
            border-radius: 12px !important;
            font-weight: 700 !important;
            padding: 0.45rem 0.85rem !important;
        }}
        div.stButton > button:hover {{
            filter: brightness(0.95);
        }}
        div.stButton > button:disabled {{
            opacity: 0.55;
        }}
        """,
    ):
        return st.button(label, *args, **kwargs)

def render_dataframe(
    df: pd.DataFrame,
    *,
    table_key: str,
    header_color: str,
    height: int | None = None,
    **kwargs,
):
    """Render a dataframe with a coloured header row.

    Streamlit's dataframe header styling can vary by version. To make this robust:
    - We apply a pandas Styler header style (works in many versions).
    - We also inject scoped CSS around the element to catch versions that ignore Styler header styles.

    Table height is used to keep long pick-lists scrollable.
    """
    anchor = f"tbl-{table_key}"
    st.markdown(f'<div id="{anchor}"></div>', unsafe_allow_html=True)

    # Scoped CSS: target any table headers rendered by st.dataframe immediately after this anchor.
    st.markdown(
        f"""
        <style>
          /* Try to scope to the next dataframe block after the anchor */
          #{anchor} + div [data-testid="stDataFrame"] thead th,
          #{anchor} + div div[data-testid="stDataFrame"] thead th,
          #{anchor} ~ div div[data-testid="stDataFrame"] thead th {{
            background: {header_color} !important;
            color: white !important;
          }}
        </style>
        """,
        unsafe_allow_html=True,
    )

    if height is not None:
        kwargs["height"] = int(height)

    # Pandas Styler approach (often works even when CSS selectors shift)
    try:
        styler = df.style.set_table_styles(
            [
                {"selector": "th", "props": [("background-color", header_color), ("color", "white")]}
            ]
        )
        return st.dataframe(styler, **kwargs)
    except Exception:
        return st.dataframe(df, **kwargs)

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
        "quick_log_prefs",
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
            "quick_log_prefs",
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
            "quick_log_prefs",
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


def upsert_apple_move_burn(conn: sqlite3.Connection, entry_date: date, kcal_burned: float) -> None:
    """Create/replace a single 'Apple Move' burn entry for a date."""
    conn.execute(
        "DELETE FROM burn_entries WHERE entry_date = ? AND name = ?",
        (entry_date.isoformat(), "Apple Move"),
    )
    conn.execute(
        """
        INSERT INTO burn_entries(entry_date, category, name, kcal_burned, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            entry_date.isoformat(),
            "Daily living",
            "Apple Move",
            float(kcal_burned),
            datetime.utcnow().isoformat(timespec="seconds"),
        ),
    )
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


def update_recipe(conn: sqlite3.Connection, recipe_id: int, name: str, servings: float, notes: str = "") -> None:
    """Update recipe metadata (name/servings/notes)."""
    conn.execute(
        "UPDATE recipes SET name = ?, servings = ?, notes = ? WHERE id = ?",
        (name.strip(), float(servings), notes.strip(), int(recipe_id)),
    )
    conn.commit()


def duplicate_recipe(conn: sqlite3.Connection, recipe_id: int, new_name: str) -> int:
    """Duplicate a recipe and all its ingredients. Returns the new recipe id."""
    r = conn.execute("SELECT name, servings, notes FROM recipes WHERE id = ?", (int(recipe_id),)).fetchone()
    if not r:
        raise ValueError("Recipe not found")

    new_id = create_recipe(conn, new_name.strip(), float(r["servings"]), str(r["notes"] or ""))

    items = conn.execute(
        """
        SELECT item, grams, kcal_per_100g, kcal, protein_g, carbs_g, fat_g, source
        FROM recipe_items
        WHERE recipe_id = ?
        """,
        (int(recipe_id),),
    ).fetchall()

    for it in items:
        conn.execute(
            """
            INSERT INTO recipe_items(recipe_id, item, grams, kcal_per_100g, kcal, protein_g, carbs_g, fat_g, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(new_id),
                it["item"],
                float(it["grams"]),
                float(it["kcal_per_100g"]),
                float(it["kcal"]),
                it["protein_g"],
                it["carbs_g"],
                it["fat_g"],
                it["source"],
            ),
        )

    conn.commit()
    return int(new_id)


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
inject_global_css()

# Button-colour system status (indicator)
try:
    if stylable_container is None:
        st.sidebar.warning("🎨 Multi-colour buttons: OFF (streamlit-extras not available)")
        st.sidebar.caption("Install with: `python3 -m pip install streamlit-extras`")
        if _STYLABLE_CONTAINER_ERR:
            st.sidebar.caption(f"Import error: {_STYLABLE_CONTAINER_ERR}")
    else:
        st.sidebar.success("🎨 Multi-colour buttons: ON")
except Exception:
    # If sidebar isn't ready for some reason, fail silently.
    pass



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

        with st.form("passcode_form"):
            entered = st.text_input("Passcode", type="password")
            submitted = st.form_submit_button("Unlock")

        if submitted:
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
    # Apply pending updates BEFORE widgets are instantiated (avoids StreamlitAPIException)
    if "_pending_daily_calorie_budget" in st.session_state:
        st.session_state["daily_calorie_budget"] = int(st.session_state.pop("_pending_daily_calorie_budget"))

    # Ensure the budget key exists so widgets can bind to it
    if "daily_calorie_budget" not in st.session_state:
#################################################################
##### DC 4/1/26 Calculate session calorie requirements      #####
##### then change this value so it persists across sessions #####
##### Default = 2000.                                       #####
#################################################################
        st.session_state["daily_calorie_budget"] = 1941 # Calculated from sidebar 1/1/26. 
    banner("⚙️ Settings", "help")
    xlsx_path = st.text_input("Food database (xlsx)", value=DEFAULT_DB_PATH)
    daily_budget = st.number_input("Daily calorie budget (default = 2000)", min_value=0, max_value=20000, step=50, key="daily_calorie_budget")


    banner("🎯 Goal planner", "help")
    st.caption("Estimate a daily calorie target to reach a goal weight by a chosen date.")

    gp_col1, gp_col2 = st.columns(2)
    with gp_col1:
        gp_sex = st.selectbox("Sex", ["Female", "Male"], index=0, key="gp_sex")
    with gp_col2:
        gp_age = st.number_input("Age", min_value=16, max_value=100, value=40, step=1, key="gp_age")

    gp_height_cm = st.number_input("Height (cm)", min_value=120, max_value=220, value=170, step=1, key="gp_height_cm")
    gp_start_kg = st.number_input("Start weight (kg)", min_value=30.0, max_value=300.0, value=80.0, step=0.1, key="gp_start_kg")
    gp_target_kg = st.number_input("Target weight (kg)", min_value=30.0, max_value=300.0, value=75.0, step=0.1, key="gp_target_kg")

    gp_target_date = st.date_input("Target date", value=date.today() + timedelta(days=90), min_value=date.today(), key="gp_target_date")

    gp_activity = st.selectbox(
        "Lifestyle / activity level",
        [
            "Sedentary (little or no exercise)",
            "Lightly active (1–3 days/week)",
            "Moderately active (3–5 days/week)",
            "Very active (6–7 days/week)",
        ],
        index=1,
        key="gp_activity",
    )

    gp_factor = {
        "Sedentary (little or no exercise)": 1.2,
        "Lightly active (1–3 days/week)": 1.375,
        "Moderately active (3–5 days/week)": 1.55,
        "Very active (6–7 days/week)": 1.725,
    }[gp_activity]

    gp_days = (gp_target_date - date.today()).days
    gp_kg_to_lose = float(gp_start_kg) - float(gp_target_kg)

    gp_suggested = None
    if gp_days > 0 and gp_kg_to_lose > 0:
        # Mifflin–St Jeor BMR
        if gp_sex == "Male":
            gp_bmr = 10 * float(gp_start_kg) + 6.25 * float(gp_height_cm) - 5 * float(gp_age) + 5
        else:
            gp_bmr = 10 * float(gp_start_kg) + 6.25 * float(gp_height_cm) - 5 * float(gp_age) - 161

        gp_tdee = gp_bmr * gp_factor
        gp_daily_deficit = (gp_kg_to_lose * 7700.0) / gp_days
        gp_suggested = gp_tdee - gp_daily_deficit

        st.write(f"**Estimated maintenance:** {int(round(gp_tdee))} kcal/day")
        st.write(f"**Required deficit:** {int(round(gp_daily_deficit))} kcal/day")
        st.write(f"**Suggested intake:** **{int(round(gp_suggested))} kcal/day**")

        if gp_daily_deficit > 1000:
            st.warning("Aggressive target. Consider extending the timeline.")
        if gp_suggested < 1200 and gp_sex == "Female":
            st.warning("Very low target. Consider extending the timeline.")
        if gp_suggested < 1500 and gp_sex == "Male":
            st.warning("Very low target. Consider extending the timeline.")

        if cb("Apply suggested calorie budget", key="btn_apply_gp", role="log"):
            st.session_state["_pending_daily_calorie_budget"] = int(round(gp_suggested))
            st.rerun()
    else:
        st.info("Enter a lower target weight and a future target date to calculate a plan.")

    banner(" Apple Move", "help")
    st.caption("Optional: log Apple Fitness 'Move' calories as burned calories (with a conservative factor).")
    apple_move_factor = st.number_input(
        "Apple Move factor",
        min_value=0.0,
        max_value=1.5,
        value=0.5,
        step=0.05,
        help="Example: enter 0.5 to count 50% of Move calories.",
        key="apple_move_factor",
    )

    banner("🎯 Macro targets (daily)", "help")
    protein_target = st.number_input("Protein target (g)", min_value=0, max_value=1000, value=120, step=5)
    carbs_target = st.number_input("Carbs target (g)", min_value=0, max_value=2000, value=250, step=10)
    fat_target = st.number_input("Fat target (g)", min_value=0, max_value=1000, value=70, step=5)

    show_kj = st.toggle("Also show kJ (approx)", value=False)

    banner("🧹 New year reset", "barcode")
    st.caption("Clear daily logs and charts (keeps favourites, recipes, saved meals, food DB).")

    ny_confirm = st.checkbox("Yes — clear all diary food + burned entries", key="ny_confirm")
    ny_clear_weight = st.checkbox("Also clear weight log", value=False, key="ny_clear_weight")

    if cb("Clear logs now", key="btn_clear_logs", role="delete", type="secondary"):
        if not ny_confirm:
            st.warning("Tick the confirmation box first.")
        else:
            try:
                import sqlite3
                with sqlite3.connect(SQLITE_PATH) as _conn:
                    _conn.execute("DELETE FROM diary_entries")
                    _conn.execute("DELETE FROM burn_entries")
                    if ny_clear_weight:
                        _conn.execute("DELETE FROM weights")
                    _conn.commit()
                st.success("Cleared. Starting fresh!")
                st.rerun()
            except Exception as _e:
                st.error(f"Couldn't clear logs: {_e}")

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

# Quick-log preferences (per food): which portion + default multiplier
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS quick_log_prefs (
        item_key TEXT PRIMARY KEY,
        item TEXT NOT NULL,
        portion_label TEXT,
        multiplier REAL NOT NULL DEFAULT 1.0,
        updated_at TEXT NOT NULL
    )
    """
)
conn.commit()

lookup = build_lookup(ingredients_df)

# Tabs
(tab_diary, tab_add, tab_recipes, tab_saved, tab_barcode, tab_weight, tab_trends, tab_help) = st.tabs(
    ["📅 Diary", "➕ Add food", "🍲 Recipes", "⭐ Saved meals", "🏷️ Barcode", "⚖️ Weight", "📈 Trends", "📘 Help"]
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
        banner("⚡ Quick actions", "quick")
        # Copy from another day
        st.write("**Copy from another day**")
        copy_from = st.date_input("Copy entries from", value=diary_date - timedelta(days=1), key="copy_from_date")
        if cb(f"Copy {copy_from.isoformat()} → {diary_date.isoformat()}", key="btn_copy_from"):
            src_entries = read_entries(conn, copy_from)
            if src_entries.empty:
                st.warning(f"No entries on {copy_from.isoformat()}.")
            else:
                for _, e in src_entries.iterrows():
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
                        source=(str(e.get("source") or "") + f" | copied:{copy_from.isoformat()}").strip(" |"),
                    )
                st.success(f"Copied {len(src_entries)} entries from {copy_from.isoformat()}.")
                st.rerun()

        # Quick add calories
        with st.expander("Quick-add calories"):
            qa_meal = st.selectbox("Meal", options=MEALS, index=3, key="qa_meal")
            qa_kcal = st.number_input("Calories to add", min_value=0.0, max_value=5000.0, value=100.0, step=10.0, key="qa_kcal")
            qa_note = st.text_input("Note (optional)", placeholder="e.g., Coffee + biscuit", key="qa_note")
            if cb("Add quick calories", key="qa_btn"):
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

    banner("🧾 Daily exercise and burn calories", "entries")

    # Log burned calories
    with st.expander("Log calories burned (exercise / activity / daily living)"):
        st.write("**Apple Move quick-entry (optional)**")
        am1, am2, am3, am4 = st.columns([1, 1, 1, 1])
        with am1:
            move_kcal = st.number_input(
                "Apple Move (kcal)",
                min_value=0.0,
                max_value=10000.0,
                value=0.0,
                step=10.0,
                key="apple_move_kcal",
                help="Enter today's Move calories from Apple Fitness.",
            )
        with am2:
            move_factor = st.number_input(
                "Factor",
                min_value=0.0,
                max_value=1.5,
                value=float(st.session_state.get("apple_move_factor", 0.5)),
                step=0.05,
                key="apple_move_factor_inline",
                help="Counts Move × factor as burned calories (conservative by default).",
            )
        with am3:
            move_burn = float(move_kcal) * float(move_factor)
            st.metric("Will log", f"{move_burn:.0f} kcal")
        with am4:
            if cb("Save/Update Apple Move", key="apple_move_save"):
                if move_kcal <= 0:
                    # If they set to 0, remove the entry entirely
                    conn.execute(
                        "DELETE FROM burn_entries WHERE entry_date = ? AND name = ?",
                        (diary_date.isoformat(), "Apple Move"),
                    )
                    conn.commit()
                    st.success("Apple Move removed for this day.")
                else:
                    upsert_apple_move_burn(conn, diary_date, float(move_burn))
                    st.success("Apple Move saved for this day.")
                st.rerun()

        st.divider()
        st.write("**Manual burn entry**")
        bc1, bc2, bc3 = st.columns([1, 2, 1])
        with bc1:
            bcat = st.selectbox("Category", options=["Exercise", "Activity", "Daily living"], key="burn_cat")
        with bc2:
            bname = st.text_input("Description", placeholder="e.g., 30 min run, brisk walk, active job", key="burn_name")
        with bc3:
            bkcal = st.number_input("kcal burned", min_value=0.0, max_value=5000.0, value=200.0, step=10.0, key="burn_kcal")

        if cb("Add burned calories", key="burn_add", type="primary"): 
            add_burn_entry(conn, diary_date, bcat, bname.strip() or bcat, float(bkcal))
            st.success("Added.")
            st.rerun()

    if not burn.empty:
        st.write("**Burn entries**")
        bdisp = burn.rename(columns={"category": "Category", "name": "Description", "kcal_burned": "kcal"})
        bdisp_show = bdisp[["id", "Category", "Description", "kcal"]].copy()
        bdisp_show["kcal"] = pd.to_numeric(bdisp_show["kcal"], errors="coerce").fillna(0).round(0).astype(int)
        render_dataframe(bdisp_show, table_key="burn_entries", header_color=BRIGHT_PALETTE["quick"], height=220, hide_index=True, width='stretch')
        with st.expander("Delete a burn entry"):
            bid = st.selectbox("Burn entry id", options=bdisp["id"].tolist(), key="burn_del_id")
            if cb("Delete burn entry", key="burn_del_btn"):
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
            mt = meal_totals.reset_index().rename(columns={"meal": "Meal", "kcal": "kcal"}).copy()
            mt["kcal"] = pd.to_numeric(mt["kcal"], errors="coerce").fillna(0).round(0).astype(int)
            render_dataframe(mt, table_key="meal_totals", header_color=BRIGHT_PALETTE["entries"], height=180, hide_index=True, width='stretch')

        display = entries.rename(
            columns={
                "meal": "Meal",
                "item": "Item",
                "grams": "Grams",
                "kcal_per_100g": "kcal/100g",
                "kcal": "kcal",
#########################################################################
##### DC edited 4/1/26 to remove protein, carbs and fat from tables #####
#########################################################################
#                "protein_g": "Protein (g)",
#                "carbs_g": "Carbs (g)",
#                "fat_g": "Fat (g)",
            }
        )

#        cols = ["id", "Meal", "Item", "Grams", "kcal/100g", "kcal", "Protein (g)", "Carbs (g)", "Fat (g)", "source"]
        cols = ["id", "Meal", "Item", "Grams", "kcal/100g", "kcal", "source"]
        disp2 = display[cols].copy()
#        for c in ["Grams","kcal/100g","kcal","Protein (g)","Carbs (g)","Fat (g)"]:
        for c in ["Grams","kcal/100g","kcal"]:
            if c in disp2.columns:
                disp2[c] = pd.to_numeric(disp2[c], errors="coerce").fillna(0).round(0).astype(int)
        render_dataframe(disp2, table_key="diary_entries", header_color=BRIGHT_PALETTE["entries"], height=360, hide_index=True, width='stretch')

        with st.expander("Delete an entry"):
            entry_ids = [int(x) for x in display["id"].tolist()] if (not display.empty and "id" in display.columns) else []
            if not entry_ids:
                st.caption("No entries to delete for this date/meal filter.")
            else:
                to_delete = st.selectbox("Select entry id", options=entry_ids, key="diary_delete_id")
                confirm_del = st.checkbox("Confirm delete", value=False, key="diary_delete_confirm")
                if cb("Delete entry", key="diary_delete_btn", role="delete") and confirm_del:
                    delete_diary_entry(conn, int(to_delete))
                    st.success("Deleted.")
                    st.rerun()


# ----------------------------
# ADD FOOD TAB
# ----------------------------
with tab_add:
    banner("🔎 Search & log", "search")

    # Ensure a consistent selected item across reruns
    if "selected_item" not in st.session_state:
        st.session_state["selected_item"] = ""

    # Favourites & Recents (clickable)
    # Streamlit 1.52 supports dataframe row selection via on_select.

    # --- Shared quick-log controls (apply to both Favourites + Recents) ---
    ql1, ql2, ql3 = st.columns([1, 1, 2])
    with ql1:
        quick_date = st.date_input("Quick log date", value=date.today(), key="quick_log_date")
    with ql2:
        quick_meal = st.selectbox("Quick log meal", options=MEALS, index=0, key="quick_log_meal")
    with ql3:
        ql_mode = st.radio(
            "Default one-click amount",
            options=["1 portion if available", "100g"],
            horizontal=True,
            key="quick_log_mode",
        )

    st.caption(
        "One-click log: logs **1 typical portion** if defined, otherwise logs **100g**. "
        "You can set a saved default portion + multiplier per food (and reset it)."
    )

    # --- Quick-log preference helpers ---
    def _get_quicklog_pref(item_key: str) -> Optional[dict]:
        row = conn.execute(
            "SELECT portion_label, multiplier FROM quick_log_prefs WHERE item_key = ?",
            (item_key,),
        ).fetchone()
        if not row:
            return None
        return {"portion_label": row[0], "multiplier": safe_float(row[1], 1.0)}


    def _set_quicklog_pref(item_key: str, item_name: str, portion_label: Optional[str], mult: float):
        conn.execute(
            """
            INSERT INTO quick_log_prefs(item_key, item, portion_label, multiplier, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(item_key) DO UPDATE SET
                item=excluded.item,
                portion_label=excluded.portion_label,
                multiplier=excluded.multiplier,
                updated_at=excluded.updated_at
            """,
            (
                item_key,
                item_name,
                portion_label,
                float(mult),
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()


    def _one_click_log(item_name: str, portion_label: Optional[str] = None, mult: float = 1.0):
        """Logs immediately using the current Quick log date/meal, and shows multiplier in the diary item text."""
        if not item_name:
            return

        r = lookup.get(item_name)
        if r is None:
            st.warning("Food not found in database.")
            return

        kcal100 = safe_float(r.get("Energy (kcal)"), 0.0)
        item_key = str(r.get("_key") or item_name.strip().lower())

        # Read meal/date from session_state (prevents 'always Lunch' bugs on rerun)
        q_date = st.session_state.get("quick_log_date", quick_date)
        q_meal = st.session_state.get("quick_log_meal", quick_meal)

        # Default diary label
        item_to_log = item_name

        # Macros from DB + override if present
        protein100 = r.get("Protein_g_100g") if "Protein_g_100g" in r.index else None
        carbs100 = r.get("Carbs_g_100g") if "Carbs_g_100g" in r.index else None
        fat100 = r.get("Fat_g_100g") if "Fat_g_100g" in r.index else None
        override = get_macro_override(conn, item_key)
        if override:
            protein100 = override.get("Protein_g_100g")
            carbs100 = override.get("Carbs_g_100g")
            fat100 = override.get("Fat_g_100g")

        grams = 100.0
        kcal = kcal_for_grams(kcal100, grams)
        source = "one-click:100g"

        if st.session_state.get("quick_log_mode", ql_mode) == "1 portion if available":
            p = portion_options_for_item(portions_df, item_name)
            if not p.empty:
                p = p.copy()
                p["_label"] = p.apply(lambda x: f"{x['Portion']} • {x['Portion (g/ml)']}g", axis=1)
                if portion_label and portion_label in p["_label"].tolist():
                    chosen = p[p["_label"] == portion_label].iloc[0]
                else:
                    chosen = p.iloc[0]

                grams_one = safe_float(chosen.get("Portion (g/ml)"), 0.0)
                kcal_one = chosen.get("Energy (kcal) per portion/item")
                kcal_one = safe_float(kcal_one, 0.0) if kcal_one is not None else kcal_for_grams(kcal100, grams_one)

                grams = grams_one * float(mult)
                kcal = kcal_one * float(mult)
                source = f"one-click:portion:{str(chosen.get('Portion'))} x{float(mult):g}"

                # Make multiplier visible in diary item text
                if abs(float(mult) - 1.0) > 1e-9:
                    item_to_log = f"{item_name} ({str(chosen.get('Portion'))} x{float(mult):g})"

        p_g, c_g, f_g = macros_for_grams(
            None if protein100 is None else safe_float(protein100, 0.0),
            None if carbs100 is None else safe_float(carbs100, 0.0),
            None if fat100 is None else safe_float(fat100, 0.0),
            grams,
        )

        add_diary_entry(
            conn,
            entry_date=q_date,
            meal=q_meal,
            item=item_to_log,
            grams=float(grams),
            kcal_per_100g=float(kcal100),
            kcal=float(kcal),
            protein_g=p_g,
            carbs_g=c_g,
            fat_g=f_g,
            source=source,
        )


    fav_col, rec_col = st.columns(2)

    # ----------------------------
    # FAVOURITES
    # ----------------------------
    with fav_col:
        st.write("**Favourite foods**")
        favs = pd.read_sql_query("SELECT * FROM favourites ORDER BY created_at DESC", conn)

        fav_filter = st.text_input(
            "Filter favourites",
            placeholder="Type to filter (e.g., bread, chicken)",
            key="fav_filter",
        )
        if fav_filter.strip():
            favs = favs[favs["item"].str.contains(fav_filter.strip(), case=False, na=False)].copy()

        if favs.empty:
            st.caption("No favourites yet. Select a food below, then favourite it.")
            picked_fav = None
        else:
            fav_view = favs[["item"]].rename(columns={"item": "Favourite"})
            fav_event = render_dataframe(
                fav_view, table_key="fav_table", header_color="#FF9500", height=260,
                hide_index=True,
                width='stretch',
                selection_mode="single-row",
                on_select="rerun",
                key="fav_table",
            )
            sel = getattr(fav_event, "selection", {}) or {}
            rows = sel.get("rows", []) or []
            picked_fav = str(favs.iloc[int(rows[0])]["item"]) if rows else None
            if picked_fav:
                st.caption(f"Selected: {picked_fav}")

            bcols = st.columns(3)
            with bcols[0]:
                if cb("Use selected", key="use_fav", role="add") and picked_fav:
                    st.session_state["food_search_q"] = picked_fav
                    st.session_state["chosen_main"] = "All"
                    st.session_state["chosen_tag"] = "All"
                    st.session_state["shelf_only"] = False
                    st.session_state["selected_item"] = picked_fav
                    st.session_state["food_select"] = picked_fav
                    st.rerun()

            fav_portion_label = None
            fav_mult = 1.0
            if picked_fav and st.session_state.get("quick_log_mode", ql_mode) == "1 portion if available":
                r = lookup.get(picked_fav)
                item_key = str((r.get("_key") if r is not None else None) or picked_fav.strip().lower())
                pref = _get_quicklog_pref(item_key)
                pref_label = (pref or {}).get("portion_label")
                pref_mult = safe_float((pref or {}).get("multiplier"), 1.0)

                pf = portion_options_for_item(portions_df, picked_fav)
                if not pf.empty:
                    pf = pf.copy()
                    pf["_label"] = pf.apply(lambda x: f"{x['Portion']} • {x['Portion (g/ml)']}g • {safe_float(x.get('Energy (kcal) per portion/item'), 0.0):.0f} kcal", axis=1)
                    labels = pf["_label"].tolist()
                    default_idx = labels.index(pref_label) if (pref_label in labels) else 0
                    fav_portion_label = st.selectbox(
                        "One-click portion (favourites)",
                        options=labels,
                        index=default_idx,
                        key=f"ql_fav_portion_{item_key}",
                    )
                    fav_mult = st.number_input(
                        "How many portions? (favourites)",
                        min_value=0.1,
                        max_value=50.0,
                        value=float(pref_mult),
                        step=0.25,
                        key=f"ql_fav_mult_{item_key}",
                    )
                    if cb("Reset saved one-click preference", key=f"reset_fav_pref_{item_key}"):
                        conn.execute("DELETE FROM quick_log_prefs WHERE item_key = ?", (item_key,))
                        conn.commit()
                        st.success("Saved one-click preference cleared.")
                        st.rerun()
                else:
                    fav_mult = float(pref_mult)

            with bcols[1]:
                if cb("⚡ Log selected", key="log_fav", role="log") and picked_fav:
                    r = lookup.get(picked_fav)
                    item_key = str((r.get("_key") if r is not None else None) or picked_fav.strip().lower())
                    _set_quicklog_pref(item_key, picked_fav, fav_portion_label, float(fav_mult))
                    _one_click_log(picked_fav, portion_label=fav_portion_label, mult=float(fav_mult))
                    st.success("Logged!")
                    st.rerun()

            with bcols[2]:
                if cb("Remove selected", key="rm_fav", role="delete") and picked_fav:
                    conn.execute("DELETE FROM favourites WHERE item_key = ?", (picked_fav.strip().lower(),))
                    conn.commit()
                    st.rerun()


    # ----------------------------
    # RECENTS
    # ----------------------------
    with rec_col:
        st.write("**Recent foods**")
        st.caption("Tip: select a recent item, then you can favourite it or one-click log it.")
        recent = pd.read_sql_query(
            """
            SELECT
              CASE
                WHEN instr(item, ' (') > 0 THEN substr(item, 1, instr(item, ' (') - 1)
                ELSE item
              END AS item,
              MAX(created_at) AS last_used
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
            rec_view = recent[["item"]].rename(columns={"item": "Recent"})
            rec_event = render_dataframe(
                rec_view, table_key="rec_table", header_color="#34C759", height=260,
                hide_index=True,
                width='stretch',
                selection_mode="single-row",
                on_select="rerun",
                key="rec_table",
            )
            sel = getattr(rec_event, "selection", {}) or {}
            rows = sel.get("rows", []) or []
            picked_rec = str(recent.iloc[int(rows[0])]["item"]) if rows else None
            if picked_rec:
                st.caption(f"Selected: {picked_rec}")
                # Quick favourite from Recents
                r = lookup.get(picked_rec)
                fav_key = str((r.get("_key") if r is not None else None) or picked_rec.strip().lower())
                exists = conn.execute("SELECT 1 FROM favourites WHERE item_key = ?", (fav_key,)).fetchone() is not None
                if cb("⭐ Unfavourite" if exists else "☆ Favourite selected", key=f"fav_from_recent_{fav_key}"):
                    if exists:
                        conn.execute("DELETE FROM favourites WHERE item_key = ?", (fav_key,))
                    else:
                        conn.execute(
                            "INSERT OR REPLACE INTO favourites(item_key, item, created_at) VALUES (?, ?, ?)",
                            (fav_key, picked_rec, datetime.utcnow().isoformat(timespec="seconds")),
                        )
                    conn.commit()
                    st.rerun()

            rcols = st.columns(2)
            with rcols[0]:
                if cb("Use selected", key="use_rec", role="add") and picked_rec:
                    st.session_state["food_search_q"] = picked_rec
                    st.session_state["chosen_main"] = "All"
                    st.session_state["chosen_tag"] = "All"
                    st.session_state["shelf_only"] = False
                    st.session_state["selected_item"] = picked_rec
                    st.session_state["food_select"] = picked_rec
                    st.rerun()

            rec_portion_label = None
            rec_mult = 1.0
            if picked_rec and st.session_state.get("quick_log_mode", ql_mode) == "1 portion if available":
                r = lookup.get(picked_rec)
                item_key = str((r.get("_key") if r is not None else None) or picked_rec.strip().lower())
                pref = _get_quicklog_pref(item_key)
                pref_label = (pref or {}).get("portion_label")
                pref_mult = safe_float((pref or {}).get("multiplier"), 1.0)

                pr = portion_options_for_item(portions_df, picked_rec)
                if not pr.empty:
                    pr = pr.copy()
                    pr["_label"] = pr.apply(lambda x: f"{x['Portion']} • {x['Portion (g/ml)']}g", axis=1)
                    labels = pr["_label"].tolist()
                    default_idx = labels.index(pref_label) if (pref_label in labels) else 0
                    rec_portion_label = st.selectbox(
                        "One-click portion (recents)",
                        options=labels,
                        index=default_idx,
                        key=f"ql_rec_portion_{item_key}",
                    )
                    rec_mult = st.number_input(
                        "How many portions? (recents)",
                        min_value=0.1,
                        max_value=50.0,
                        value=float(pref_mult),
                        step=0.25,
                        key=f"ql_rec_mult_{item_key}",
                    )
                    if cb("Reset saved one-click preference", key=f"reset_rec_pref_{item_key}"):
                        conn.execute("DELETE FROM quick_log_prefs WHERE item_key = ?", (item_key,))
                        conn.commit()
                        st.success("Saved one-click preference cleared.")
                        st.rerun()
                else:
                    rec_mult = float(pref_mult)

            with rcols[1]:
                if cb("⚡ Log selected", key="log_rec", role="log") and picked_rec:
                    r = lookup.get(picked_rec)
                    item_key = str((r.get("_key") if r is not None else None) or picked_rec.strip().lower())
                    _set_quicklog_pref(item_key, picked_rec, rec_portion_label, float(rec_mult))
                    _one_click_log(picked_rec, portion_label=rec_portion_label, mult=float(rec_mult))
                    st.success("Logged!")
                    st.rerun()

        st.divider()

        left, right = st.columns([2, 1])
        with left:
            q = st.text_input(
                "Search foods",
                placeholder="e.g., chicken breast, basmati rice, olive oil",
                key="food_search_q",
            )
        with right:
            chosen_main = st.selectbox("Category", options=main_categories, key="chosen_main")
            chosen_tag = st.selectbox("Cuisine tag", options=cuisine_tags, key="chosen_tag")
            shelf_only = st.toggle("Shelf-stable only", value=False, key="shelf_only")

        results = search_foods(ingredients_df, q, chosen_main, chosen_tag, shelf_only)
        st.caption(f"Showing {len(results)} matching foods")

        # Show only the columns you actually need in the picker table
        results_view = results.copy()
        kcal_col = None
        for c in ["Energy (kcal)", "Energy_kcal", "kcal", "Calories", "Energy"]:
            if c in results_view.columns:
                kcal_col = c
                break
        keep = ["Item"] + ([kcal_col] if kcal_col else [])
        results_view = results_view[keep].copy() if all(k in results_view.columns for k in keep) else results_view[["Item"]].copy()
        if kcal_col and kcal_col in results_view.columns:
            results_view = results_view.rename(columns={kcal_col: "kcal"})
            results_view["kcal"] = pd.to_numeric(results_view["kcal"], errors="coerce").fillna(0).round(0).astype(int)
        # Click a row to select it
        res_event = render_dataframe(
            results_view, table_key="results_table", header_color="#00B8D9", height=320,
            width='stretch',
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun",
            key="results_table",
        )
        sel = getattr(res_event, "selection", {}) or {}
        rows = sel.get("rows", []) or []
        if rows:
            picked = str(results_view.iloc[int(rows[0])]["Item"])
            st.session_state["selected_item"] = picked
            # Also force the selectbox selection
            st.session_state["food_select"] = picked
            st.caption(f"Selected: {picked}")

        st.divider()

        banner("✅ Log selected food", "log")
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
            if cb(fav_label, key="fav_toggle"):
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
                if cb("Save macro override"):
                    upsert_macro_override(conn, item_key, p_in, c_in, f_in)
                    st.success("Saved macro override.")
                    st.rerun()

            c1, c2, c3 = st.columns([1, 1, 1])
            with c1:
                entry_date = st.date_input("Log date", value=date.today(), key="log_date")
            with c2:
                meal = st.selectbox("Meal", options=MEALS, index=0)
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
                        min_value=0.1,
                        max_value=50.0,
                        value=1.0,
                        step=0.25,
                        help="Use decimals for fractional portions (e.g., 0.5 slice, 1.5 biscuits).",
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

            if cb("Log to diary", type="primary", key="btn_log_to_diary_addfood", role="log"):
                try:
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
                    conn.commit()  # <-- add this (important if add_diary_entry doesn't commit internally)
                    st.success("Logged!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Couldn't log to diary: {e}")
                    st.exception(e)

#            if cb("Log to diary", type="primary", key="btn_log_to_diary_addfood", role="log"):
#                add_diary_entry(
#                    conn,
#                    entry_date=entry_date,
#                    meal=meal,
#                    item=selected_item,
#                    grams=float(grams),
#                    kcal_per_100g=float(kcal100),
#                    kcal=float(kcal),
#                    protein_g=p_g,
#                    carbs_g=c_g,
#                    fat_g=f_g,
#                    source=source,
#                )
#                st.success("Logged!")
#                st.rerun()


# ----------------------------
# RECIPES TAB
# ----------------------------
with tab_recipes:
    banner("🍲 Custom recipes", "recipes")

    rc1, rc2 = st.columns([1, 1])
    with rc1:
        st.write("**Create a recipe**")
        new_name = st.text_input("Recipe name", key="recipe_name")
        new_servings = st.number_input("Servings", min_value=1.0, max_value=100.0, value=4.0, step=1.0)
        new_notes = st.text_area("Notes (optional)", key="recipe_notes")
        if cb("Create recipe", key="btn_create_recipe", role="create"):
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

            # ---- Edit / duplicate recipe (metadata) ----
            with st.expander("Edit recipe details (name / servings / notes)"):
                en1, en2 = st.columns([2, 1])
                with en1:
                    edit_name = st.text_input(
                        "Recipe name",
                        value=str(recipe_row.get("name") or ""),
                        key=f"edit_recipe_name_{selected_recipe_id}",
                    )
                with en2:
                    edit_servings = st.number_input(
                        "Servings",
                        min_value=0.1,
                        max_value=1000.0,
                        value=float(servings if servings > 0 else 1.0),
                        step=0.5,
                        key=f"edit_recipe_servings_{selected_recipe_id}",
                    )

                edit_notes = st.text_area(
                    "Notes (optional)",
                    value=str(recipe_row.get("notes") or ""),
                    key=f"edit_recipe_notes_{selected_recipe_id}",
                )

                ec1, ec2 = st.columns([1, 1])
                with ec1:
                    if cb("Save changes", key=f"btn_save_recipe_{selected_recipe_id}"):
                        if not edit_name.strip():
                            st.warning("Recipe name cannot be blank.")
                        else:
                            update_recipe(conn, selected_recipe_id, edit_name, float(edit_servings), edit_notes)
                            st.success("Recipe updated.")
                            st.rerun()

                with ec2:
                    dup_name_default = f"{str(recipe_row.get('name') or '').strip()} (copy)"
                    dup_name = st.text_input(
                        "Duplicate as",
                        value=dup_name_default,
                        key=f"dup_recipe_name_{selected_recipe_id}",
                    )
                    if cb("Duplicate recipe", key=f"btn_dup_recipe_{selected_recipe_id}"):
                        if not dup_name.strip():
                            st.warning("Please enter a name for the duplicated recipe.")
                        else:
                            new_id = duplicate_recipe(conn, selected_recipe_id, dup_name)
                            st.success(f"Duplicated recipe as '{dup_name}' (id {new_id}).")
                            st.rerun()

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

                if cb("Add to recipe", type="primary", role="fav"):
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
#########################################################################
##### DC edited 4/1/26 to remove protein, carbs and fat from tables #####
#########################################################################
#                ridf = items_df[["id", "item", "grams", "kcal", "protein_g", "carbs_g", "fat_g"]].rename(
                ridf = items_df[["id", "item", "grams", "kcal",]].rename(
                    columns={
                        "item": "Item",
                        "grams": "Grams",
                        "kcal": "kcal",
#                        "protein_g": "Protein (g)",
#                        "carbs_g": "Carbs (g)",
#                        "fat_g": "Fat (g)",
                    }
                ).copy()
#                for c in ["Grams", "kcal", "Protein (g)", "Carbs (g)", "Fat (g)"]:
                for c in ["Grams", "kcal"]:
                    if c in ridf.columns:
                        ridf[c] = pd.to_numeric(ridf[c], errors="coerce").fillna(0).round(0).astype(int)
                render_dataframe(ridf, table_key="recipe_items", header_color=BRIGHT_PALETTE["recipes"], height=260, hide_index=True, width='stretch')
                with st.expander("Delete ingredient"):
                    rid_list = items_df["id"].tolist()
                    del_id = st.selectbox("Ingredient id", options=rid_list, key="del_recipe_item")
                    if cb("Delete ingredient", key="btn_del_recipe_item"):
                        delete_recipe_item(conn, int(del_id))
                        st.success("Deleted.")
                        st.rerun()
#########################################################################

            st.divider()
            st.write("**Log this recipe**")
            ld1, ld2, ld3 = st.columns(3)
            with ld1:
                log_date = st.date_input("Log date", value=date.today(), key="log_recipe_date")
            with ld2:
                log_meal = st.selectbox("Meal", options=MEALS, index=2, key="log_recipe_meal")
            with ld3:
                servings_eaten = st.number_input("Servings eaten", min_value=0.1, max_value=50.0, value=1.0, step=0.5)

            if cb("Log recipe to diary", type="primary", key="btn_log_recipe"):
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
                if cb("Delete this recipe", type="secondary"):
                    delete_recipe(conn, selected_recipe_id)
                    st.success("Deleted recipe.")
                    st.rerun()


# ----------------------------
# SAVED MEALS TAB
# ----------------------------
with tab_saved:
    banner("⭐ Saved meals (templates)", "saved")
    st.caption("Save a frequently repeated meal and re-log it in one click.")

    sm1, sm2 = st.columns([1, 2])
    with sm1:
        st.write("**Create saved meal**")
        sm_name = st.text_input("Name", key="sm_name")
        sm_meal = st.selectbox("Default meal", options=MEALS, index=1, key="sm_meal")
        if cb("Create saved meal"):
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
                st.dataframe(items[["item", "grams", "kcal", "protein_g", "carbs_g", "fat_g"]], hide_index=True, width='stretch')

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

                if cb("Add item", key="btn_add_sm"):
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

            if cb("Log to diary", type="primary", key="btn_log_sm"):
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
                if cb("Delete this saved meal", type="secondary", key="btn_del_sm"):
                    delete_saved_meal(conn, chosen_id)
                    st.success("Deleted.")
                    st.rerun()


# ----------------------------
# BARCODE TAB
# ----------------------------
with tab_barcode:
    banner("🏷️ Barcode lookup (packaged foods)", "barcode")
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
        banner("✅ Log this product", "barcode")
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

        if cb("Log barcode food", type="primary"):
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
    banner("⚖️ Weight tracking", "weight")

    w1, w2 = st.columns([1, 2])
    with w1:
        w_date = st.date_input("Date", value=date.today(), key="w_date")
        w_val = st.number_input("Weight (kg)", min_value=0.0, max_value=500.0, value=75.0, step=0.1)
        if cb("Save weight", key="btn_save_weight", role="log"):
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
    banner("📈 Weekly trend charts", "trends")

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
        st.dataframe(weekly.reset_index().rename(columns={"entry_date": "Week"}), hide_index=True, width='stretch')

        # Merge weight if present
        weights_df = pd.read_sql_query("SELECT * FROM weights ORDER BY entry_date", conn)
        if not weights_df.empty:
            weights_df["entry_date"] = pd.to_datetime(weights_df["entry_date"])
            st.write("**Weight (with 7-day average)**")
            w = weights_df.set_index("entry_date")["weight_kg"].asfreq("D").interpolate(limit_direction="both")
            w_roll = w.rolling(7, min_periods=1).mean()
            st.line_chart(pd.DataFrame({"weight": w, "weight_7d_avg": w_roll}))


# ----------------------------
# HELP TAB (Beginner user manual)
# ----------------------------
with tab_help:
    banner("📘 User manual (beginner-friendly)", "help")
    st.markdown(
        """
## 1) What this app is
This is a simple calorie diary similar to Lose It!:
- Log foods by **grams** or **typical portions**
- Track **calories** (and macros when available)
- Track **burned calories** (exercise/activity/daily living)
- Save **recipes** and **saved meals**
- Log packaged foods by **barcode**
- Track **weight** and view **trends**

## 2) Using the app on laptop + iPhone
- Open the **same Streamlit URL** on both devices.
- Everything you log goes into the hosted diary database.

### Passcode (optional)
If you set a passcode in Streamlit Secrets, you must enter it to use the app.

## 3) Diary tab (📅)
### Daily totals
At the top you’ll see:
- **Consumed** = food calories
- **Burned** = calories you logged as exercise/activity/daily living
- **Net** = Consumed − Burned
- **Remaining** = Budget − Net

### Copy from another day
Use **Copy entries from** to copy any previous day into the selected date.

### Quick-add calories
Use this for quick entries when you don’t want to search a food (e.g., “coffee”).

### Burned calories
Use **Log calories burned** to subtract calories (exercise/activity/daily living).

#### Apple Move quick-entry (optional)
If you use Apple Fitness, you can enter your daily **Move** calories and a **factor** (e.g., 0.5). The app logs:
- **Burned = Move × factor**
This is a conservative way to avoid overestimating burn.
- Click **Save/Update Apple Move** to replace the entry for that day.
- Set Move to **0** and save to remove it for that day.

## 4) Add food tab (➕)
This is the main logging screen.

### Search and filters
- Use the search box to find foods.
- Filter by Category/Cuisine/Shelf-stable.
- You can click a row in the results table to select it.

### Log by grams
Choose **Grams** and enter the amount.

### Log by typical portion
Choose **Typical portion** and select a portion (e.g., “1 slice”).
- You can log multiple or fractional portions (e.g., 2, 1.5, 0.5).

### Favourites and Recents
- Click a row to select it.
- **Use selected** loads it into the logging controls.
- **⚡ Log selected** logs immediately.

#### One-click logging preferences (new)
If a food has portions defined, you can choose:
- Which portion to use (e.g., “1 slice”)
- How many portions (e.g., 2 or 0.5)

When you click **⚡ Log selected**, the app remembers this for next time.

**Resetting a saved preference**
If your usual portion changes, click **Reset saved one-click preference** under the portion controls. This clears the saved default for that food.

## 5) Recipes tab (🍲)
- Create a recipe and set servings.
- Add ingredients (grams).
- Log servings eaten to the diary.

## 6) Saved meals tab (⭐)
- Create a saved meal template.
- Add items.
- Log the whole saved meal to the diary with one click (optionally scaled).

## 7) Barcode tab (🏷️)
- Enter a barcode to fetch nutrition from Open Food Facts.
- Log grams eaten.

## 8) Weight tab (⚖️)
- Enter your weight (kg) for a date.
- View a weight chart.

## 9) Trends tab (📈)
Choose a date range to see:
- Consumed/burned/net calories
- 7-day averages
- Weekly totals
- Weight trend if you have weights logged

## 10) Backup/restore (JSON — works on iPhone)
Use **Backup / restore (JSON — works on iPhone)** at the bottom:
- **Download backup (JSON)** regularly
- **Restore** by uploading the JSON file

Tip: store the JSON file in iCloud Drive / OneDrive so you can restore from phone if needed.
        """
    )


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
    if uploaded_json and confirm_json and cb("Restore JSON backup now", type="primary", key="btn_restore_json"):
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
    if uploaded_db and confirm and cb("Restore backup now", type="primary"):
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