import os
import math
import json
from uuid import uuid4
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus
from streamlit_js_eval import streamlit_js_eval

# =========================
# 1. 讀取本機 .env（本機開發用）
# =========================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")
if os.path.exists(ENV_PATH):
    load_dotenv(ENV_PATH)

# =========================
# 2. 統一讀取設定：優先讀 Streamlit secrets，沒有才讀 .env
# =========================
def get_config(key, default=None):
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)

MYSQL_HOST = get_config("MYSQL_HOST")
MYSQL_PORT = get_config("MYSQL_PORT")
MYSQL_DB = get_config("MYSQL_DB")
MYSQL_USER = get_config("MYSQL_USER")
MYSQL_PASSWORD = get_config("MYSQL_PASSWORD", "") or ""
MYSQL_CHARSET = get_config("MYSQL_CHARSET", "utf8mb4")

pwd_escaped = quote_plus(MYSQL_PASSWORD)


# =========================
# 3. 建立資料庫連線
# =========================
@st.cache_resource
def get_engine():
    engine = create_engine(
        f"mysql+pymysql://{MYSQL_USER}:{pwd_escaped}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}?charset={MYSQL_CHARSET}",
        pool_pre_ping=True,
    )
    return engine


# =========================
# 4. 初始化問卷資料表
# =========================
@st.cache_resource
def ensure_response_table_exists():
    ddl = """
    CREATE TABLE IF NOT EXISTS experiment_questionnaire_responses (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        response_uuid VARCHAR(36) NOT NULL UNIQUE,
        submitted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        system_mode VARCHAR(1) NOT NULL,
        location_used TINYINT NOT NULL DEFAULT 0,
        user_lat DECIMAL(10,7) NULL,
        user_lon DECIMAL(10,7) NULL,
        food_w TINYINT NULL,
        service_w TINYINT NULL,
        atmosphere_w TINYINT NULL,
        price_w TINYINT NULL,
        green_w TINYINT NULL,
        geo_w TINYINT NULL,
        overall_w TINYINT NULL,
        recommendation_snapshot_json LONGTEXT NULL,
        us1 TINYINT NOT NULL,
        us2 TINYINT NOT NULL,
        us3 TINYINT NOT NULL,
        pu1 TINYINT NOT NULL,
        pu2 TINYINT NOT NULL,
        pu3 TINYINT NOT NULL,
        pu4 TINYINT NOT NULL,
        tr1 TINYINT NOT NULL,
        tr2 TINYINT NOT NULL,
        tr3 TINYINT NOT NULL,
        feedback_text TEXT NULL
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
    """
    with get_engine().begin() as conn:
        conn.execute(text(ddl))
    return True


# =========================
# 5. 讀取推薦基礎表
# =========================
@st.cache_data
def load_recommendation_base():
    query = """
    SELECT *
    FROM restaurant_recommendation_base
    ORDER BY restid
    """
    return pd.read_sql(query, get_engine())


# =========================
# 6. Haversine 距離（公里）
# =========================
def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)

    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


# =========================
# 7. 距離轉 geo_score
#    0 km -> 5 分
#    20 km 以上 -> 1 分
# =========================
def distance_to_geo_score(distance_km, max_km=20):
    clipped = min(distance_km, max_km)
    score = 5 - (clipped / max_km) * 4
    return round(max(1.0, min(5.0, score)), 2)


# =========================
# 8. 推薦分數計算
# =========================
def calculate_recommendation_score(
    df,
    system_mode,
    geo_w,
    use_geo=False,
    user_lat=None,
    user_lon=None,
    food_w=0,
    service_w=0,
    atmosphere_w=0,
    price_w=0,
    green_w=0,
    overall_w=0,
):
    result = df.copy()

    result["food_star_filled"] = result["food_star"].fillna(3.0)
    result["service_star_filled"] = result["service_star"].fillna(3.0)
    result["atmosphere_star_filled"] = result["atmosphere_star"].fillna(3.0)
    result["price_star_filled"] = result["price_star"].fillna(3.0)
    result["green_star_filled"] = result["green_star"].fillna(3.0)

    if system_mode == "A":
        result = result[result["overall_rating"].notna()].copy()

    if use_geo and user_lat is not None and user_lon is not None:
        result["distance_km"] = result.apply(
            lambda row: haversine_km(user_lat, user_lon, row["latitude"], row["longitude"]),
            axis=1,
        )
        result["distance_km"] = result["distance_km"].round(2)
        result["geo_score"] = result["distance_km"].apply(distance_to_geo_score)
    else:
        result["distance_km"] = None
        result["geo_score"] = None

    if system_mode == "A":
        total_weight = overall_w + (geo_w if use_geo else 0)
        if total_weight == 0:
            total_weight = 1

        overall_w_n = overall_w / total_weight
        geo_w_n = (geo_w / total_weight) if use_geo else 0

        result["final_score"] = result["overall_rating"] * overall_w_n
        if use_geo:
            result["final_score"] = result["final_score"] + result["geo_score"] * geo_w_n

    elif system_mode == "B":
        total_weight = food_w + service_w + atmosphere_w + price_w + (geo_w if use_geo else 0)
        if total_weight == 0:
            total_weight = 1

        food_w_n = food_w / total_weight
        service_w_n = service_w / total_weight
        atmosphere_w_n = atmosphere_w / total_weight
        price_w_n = price_w / total_weight
        geo_w_n = (geo_w / total_weight) if use_geo else 0

        result["final_score"] = (
            result["food_star_filled"] * food_w_n
            + result["service_star_filled"] * service_w_n
            + result["atmosphere_star_filled"] * atmosphere_w_n
            + result["price_star_filled"] * price_w_n
        )

        if use_geo:
            result["final_score"] = result["final_score"] + result["geo_score"] * geo_w_n

    elif system_mode == "C":
        total_weight = food_w + service_w + atmosphere_w + price_w + green_w + (geo_w if use_geo else 0)
        if total_weight == 0:
            total_weight = 1

        food_w_n = food_w / total_weight
        service_w_n = service_w / total_weight
        atmosphere_w_n = atmosphere_w / total_weight
        price_w_n = price_w / total_weight
        green_w_n = green_w / total_weight
        geo_w_n = (geo_w / total_weight) if use_geo else 0

        result["final_score"] = (
            result["food_star_filled"] * food_w_n
            + result["service_star_filled"] * service_w_n
            + result["atmosphere_star_filled"] * atmosphere_w_n
            + result["price_star_filled"] * price_w_n
            + result["green_star_filled"] * green_w_n
        )

        if use_geo:
            result["final_score"] = result["final_score"] + result["geo_score"] * geo_w_n

    return result.sort_values(by="final_score", ascending=False)


# =========================
# 9. 前端顯示表格
# =========================
def build_top10_display(ranked_df, system_mode):
    if system_mode == "A":
        display_cols = [
            "restid",
            "name",
            "city",
            "address",
            "overall_rating",
            "distance_km",
            "geo_score",
            "final_score",
        ]
    elif system_mode == "B":
        display_cols = [
            "restid",
            "name",
            "city",
            "address",
            "food_star",
            "service_star",
            "atmosphere_star",
            "price_star",
            "distance_km",
            "geo_score",
            "final_score",
        ]
    else:
        display_cols = [
            "restid",
            "name",
            "city",
            "address",
            "food_star",
            "service_star",
            "atmosphere_star",
            "price_star",
            "green_star",
            "distance_km",
            "geo_score",
            "final_score",
        ]

    top10 = ranked_df[display_cols].head(10).copy()

    one_decimal_cols = [
        "overall_rating",
        "food_star",
        "service_star",
        "atmosphere_star",
        "price_star",
        "green_star",
    ]
    two_decimal_cols = ["distance_km", "geo_score"]
    three_decimal_cols = ["final_score"]

    for col in one_decimal_cols:
        if col in top10.columns:
            top10[col] = pd.to_numeric(top10[col], errors="coerce").apply(
                lambda x: f"{x:.1f}" if pd.notna(x) else "-"
            )

    for col in two_decimal_cols:
        if col in top10.columns:
            top10[col] = pd.to_numeric(top10[col], errors="coerce").apply(
                lambda x: f"{x:.2f}" if pd.notna(x) else "-"
            )

    for col in three_decimal_cols:
        if col in top10.columns:
            top10[col] = pd.to_numeric(top10[col], errors="coerce").apply(
                lambda x: f"{x:.3f}" if pd.notna(x) else "-"
            )

    rename_map = {
        "name": "餐廳名稱",
        "city": "縣市",
        "address": "地址",
        "overall_rating": "整體評分",
        "food_star": "食物評分",
        "service_star": "服務評分",
        "atmosphere_star": "氣氛評分",
        "price_star": "價格評分",
        "green_star": "綠色評分",
        "distance_km": "距離(km)",
        "geo_score": "地理分數",
        "final_score": "推薦總分",
    }
    top10 = top10.rename(columns=rename_map)
    top10.insert(0, "推薦排名", range(1, len(top10) + 1))
    return top10


# =========================
# 10. 實驗快照
# =========================
def build_experiment_snapshot(ranked_df, system_mode, weights, use_geo, user_lat, user_lon):
    top10 = ranked_df.head(10).copy()
    snapshot = {
        "system_mode": system_mode,
        "location_used": bool(use_geo),
        "user_lat": user_lat,
        "user_lon": user_lon,
        "weights": weights,
        "top10": [],
    }

    for idx, (_, row) in enumerate(top10.iterrows(), start=1):
        snapshot["top10"].append(
            {
                "rank": idx,
                "restid": None if pd.isna(row.get("restid")) else int(row.get("restid")),
                "name": row.get("name"),
                "city": row.get("city"),
                "address": row.get("address"),
                "final_score": None if pd.isna(row.get("final_score")) else round(float(row.get("final_score")), 4),
            }
        )

    return snapshot


# =========================
# 11. 儲存問卷結果
# =========================
def save_questionnaire_response(snapshot, answers, feedback_text):
    response_uuid = str(uuid4())
    weights = snapshot.get("weights", {})
    insert_sql = text(
        """
        INSERT INTO experiment_questionnaire_responses (
            response_uuid, system_mode, location_used, user_lat, user_lon,
            food_w, service_w, atmosphere_w, price_w, green_w, geo_w, overall_w,
            recommendation_snapshot_json,
            us1, us2, us3,
            pu1, pu2, pu3, pu4,
            tr1, tr2, tr3,
            feedback_text
        ) VALUES (
            :response_uuid, :system_mode, :location_used, :user_lat, :user_lon,
            :food_w, :service_w, :atmosphere_w, :price_w, :green_w, :geo_w, :overall_w,
            :recommendation_snapshot_json,
            :us1, :us2, :us3,
            :pu1, :pu2, :pu3, :pu4,
            :tr1, :tr2, :tr3,
            :feedback_text
        )
        """
    )

    payload = {
        "response_uuid": response_uuid,
        "system_mode": snapshot.get("system_mode"),
        "location_used": 1 if snapshot.get("location_used") else 0,
        "user_lat": snapshot.get("user_lat"),
        "user_lon": snapshot.get("user_lon"),
        "food_w": weights.get("food_w"),
        "service_w": weights.get("service_w"),
        "atmosphere_w": weights.get("atmosphere_w"),
        "price_w": weights.get("price_w"),
        "green_w": weights.get("green_w"),
        "geo_w": weights.get("geo_w"),
        "overall_w": weights.get("overall_w"),
        "recommendation_snapshot_json": json.dumps(snapshot, ensure_ascii=False),
        "us1": answers["US1"],
        "us2": answers["US2"],
        "us3": answers["US3"],
        "pu1": answers["PU1"],
        "pu2": answers["PU2"],
        "pu3": answers["PU3"],
        "pu4": answers["PU4"],
        "tr1": answers["TR1"],
        "tr2": answers["TR2"],
        "tr3": answers["TR3"],
        "feedback_text": feedback_text.strip() if feedback_text else None,
    }

    with get_engine().begin() as conn:
        conn.execute(insert_sql, payload)

    return response_uuid


# =========================
# 12. UI 元件
# =========================
def inject_global_styles():
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 2rem;
            padding-bottom: 2rem;
        }
        .hero-card {
            background: linear-gradient(135deg, #edf8f1 0%, #f8fcf8 100%);
            border: 1px solid #d6eadb;
            border-radius: 24px;
            padding: 32px 36px;
            margin-bottom: 1.2rem;
            box-shadow: 0 8px 22px rgba(22, 101, 52, 0.08);
        }
        .hero-title {
            font-size: 2.3rem;
            font-weight: 800;
            color: #16301d;
            margin-bottom: 0.6rem;
            line-height: 1.25;
        }
        .hero-subtitle {
            font-size: 1.05rem;
            color: #2f4f37;
            line-height: 1.9;
        }
        .mini-badge {
            display: inline-block;
            background: #dff3e5;
            color: #1f6a38;
            border-radius: 999px;
            padding: 8px 14px;
            margin-right: 8px;
            margin-bottom: 10px;
            font-weight: 700;
            font-size: 0.92rem;
        }
        .feature-card {
            background: #ffffff;
            border: 1px solid #e5efe8;
            border-radius: 20px;
            padding: 22px 20px;
            min-height: 220px;
            box-shadow: 0 6px 18px rgba(15, 23, 42, 0.04);
        }
        .feature-icon {
            font-size: 1.8rem;
            margin-bottom: 0.4rem;
        }
        .feature-title {
            font-size: 1.15rem;
            font-weight: 800;
            color: #14301f;
            margin-bottom: 0.4rem;
        }
        .feature-text {
            font-size: 1rem;
            line-height: 1.8;
            color: #334155;
        }
        .flow-card {
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 18px;
            padding: 18px;
            min-height: 140px;
        }
        .flow-step {
            font-size: 0.88rem;
            font-weight: 800;
            color: #166534;
            margin-bottom: 0.45rem;
        }
        .flow-title {
            font-size: 1.02rem;
            font-weight: 800;
            color: #1e293b;
            margin-bottom: 0.35rem;
        }
        .flow-text {
            color: #475569;
            line-height: 1.7;
        }
        .section-title {
            font-size: 1.45rem;
            font-weight: 800;
            color: #1e293b;
            margin-top: 0.6rem;
            margin-bottom: 0.6rem;
        }
        .summary-chip {
            display: inline-block;
            margin-right: 10px;
            margin-bottom: 10px;
            padding: 8px 14px;
            border-radius: 999px;
            background: #eff6ff;
            color: #1d4ed8;
            font-weight: 700;
        }
        .survey-card {
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 18px;
            padding: 18px 18px 12px 18px;
            margin-bottom: 14px;
        }
        .survey-section-title {
            font-size: 1.2rem;
            font-weight: 800;
            color: #1e293b;
            margin-top: 1rem;
            margin-bottom: 0.5rem;
        }
        .survey-hint {
            color: #64748b;
            font-size: 0.95rem;
            margin-bottom: 1rem;
        }
        .recommend-table-wrap {
            width: 100%;
            overflow-x: auto;
            margin-top: 0.5rem;
        }
        .recommend-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 18px;
            background-color: white;
        }
        .recommend-table thead th {
            background-color: #f3f6fa;
            color: #1f2a44;
            font-weight: 700;
            font-size: 19px;
            padding: 14px 12px;
            border: 1px solid #d9e2ec;
            text-align: center;
            white-space: nowrap;
        }
        .recommend-table tbody td {
            padding: 13px 12px;
            border: 1px solid #e5e7eb;
            text-align: center;
            white-space: nowrap;
            vertical-align: middle;
        }
        .recommend-table tbody tr:nth-child(even) {
            background-color: #fafafa;
        }
        .recommend-table tbody td:nth-child(3),
        .recommend-table tbody td:nth-child(5) {
            text-align: left;
        }
        .recommend-table thead th:nth-child(5),
        .recommend-table tbody td:nth-child(5) {
            min-width: 360px;
            white-space: normal;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_static_recommendation_table(display_df):
    table_html = display_df.to_html(index=False, classes="recommend-table", border=0, escape=True)
    st.markdown(f'<div class="recommend-table-wrap">{table_html}</div>', unsafe_allow_html=True)


VALID_PAGES = {"intro", "recommend", "survey", "thank_you"}


def get_page_from_query(default_page="intro"):
    try:
        page_from_query = str(st.query_params.get("page", default_page)).strip().lower()
    except Exception:
        page_from_query = default_page
    return page_from_query if page_from_query in VALID_PAGES else default_page


def sync_query_route(mode_value, page_value):
    try:
        current_mode = str(st.query_params.get("mode", "")).upper()
    except Exception:
        current_mode = ""
    try:
        current_page = str(st.query_params.get("page", "")).lower()
    except Exception:
        current_page = ""

    route_changed = False
    if current_mode != mode_value:
        st.query_params["mode"] = mode_value
        route_changed = True
    if current_page != page_value:
        st.query_params["page"] = page_value
        route_changed = True
    return route_changed


def go_to_page(page_name):
    page_name = page_name if page_name in VALID_PAGES else "intro"
    st.session_state["page"] = page_name
    mode_value = st.session_state.get("system_mode", "C")
    sync_query_route(mode_value, page_name)
    st.rerun()


def render_intro_page(df):
    st.markdown(
        f"""
        <div class="hero-card">
            <div class="mini-badge">綠色餐廳推薦</div>
            <div class="mini-badge">評論分析</div>
            <div class="mini-badge">個人化推薦</div>
            <div class="hero-title">歡迎使用綠色餐廳推薦系統</div>
            <div class="hero-subtitle">
                這個系統會把大量餐廳評論整理成容易理解的資訊，並依照你的偏好，
                從食物、服務、氣氛、價格、綠色表現與地理位置等面向，提供較符合需求的推薦結果。<br>
                你可以把「綠色餐廳」想成一種更重視健康、環境友善與資源效率的餐廳：
                例如更留意食材來源、節能節水、減少一次性用品與降低食物浪費。
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="section-title">先用最簡單的方式認識綠色餐廳</div>', unsafe_allow_html=True)
    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown(
            """
            <div class="feature-card">
                <div class="feature-icon">🥬</div>
                <div class="feature-title">它不只是在賣沙拉</div>
                <div class="feature-text">
                    綠色餐廳不是只賣健康餐，也不是只有蔬食才算。
                    他的核心概念是：在食材、菜單設計、營運方式與用餐環境上，
                    盡量兼顧健康、環保與永續。
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with col2:
        st.markdown(
            """
            <div class="feature-card">
                <div class="feature-icon">💧⚡</div>
                <div class="feature-title">重點在日常營運細節</div>
                <div class="feature-text">
                    一家餐廳是否夠「綠」，常會反映在節能、節水、減少浪費、
                    降低一次性用品、重視食材與供應鏈管理等做法。
                    這些細節比一句口號更有說服力。
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with col3:
        st.markdown(
            """
            <div class="feature-card">
                <div class="feature-icon">🌍</div>
                <div class="feature-title">和消費者也有關</div>
                <div class="feature-text">
                    當餐廳更有效管理食材、能源與廢棄物時，通常也有機會減少資源浪費。
                    對消費者來說，在選餐廳時，除了好不好吃，也能多看一個更有意義的面向。
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown('<div class="section-title">這個系統怎麼操作？</div>', unsafe_allow_html=True)
    f1, f2, f3, f4 = st.columns(4)
    flows = [
        ("STEP 1", "設定偏好", "依照自己在意的面向，調整不同構面的權重。"),
        ("STEP 2", "取得位置", "選擇開啟定位，讓系統把距離納入推薦邏輯。"),
        ("STEP 3", "查看推薦", "系統會整理出 Top 10 推薦結果，幫你快速比較。"),
        ("STEP 4", "填寫問卷", "體驗完成後進入問卷頁，回饋使用感受以及信任程度。"),
    ]
    for col, (step, title, text_value) in zip([f1, f2, f3, f4], flows):
        with col:
            st.markdown(
                f"""
                <div class="flow-card">
                    <div class="flow-step">{step}</div>
                    <div class="flow-title">{title}</div>
                    <div class="flow-text">{text_value}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.info(f"目前系統資料庫中共可讀取 {len(df)} 家餐廳資料。")
    st.caption("問卷資料將以匿名方式儲存，不會要求填寫姓名；系統主要記錄推薦版本、操作設定與作答結果。")

    if st.button("開始使用推薦系統", use_container_width=True, type="primary"):
        go_to_page("recommend")


def render_sidebar(system_mode):
    st.sidebar.header("請設定你的偏好權重")

    if system_mode == "A":
        overall_w = st.sidebar.slider("整體評分", 0, 10, 5)
        geo_w = st.sidebar.slider("地理位置", 0, 10, 5)
        food_w = service_w = atmosphere_w = price_w = green_w = 0
    elif system_mode == "B":
        food_w = st.sidebar.slider("食物", 0, 10, 5)
        service_w = st.sidebar.slider("服務", 0, 10, 5)
        atmosphere_w = st.sidebar.slider("氣氛", 0, 10, 5)
        price_w = st.sidebar.slider("價格", 0, 10, 5)
        geo_w = st.sidebar.slider("地理位置", 0, 10, 5)
        overall_w = green_w = 0
    else:
        food_w = st.sidebar.slider("食物", 0, 10, 5)
        service_w = st.sidebar.slider("服務", 0, 10, 5)
        atmosphere_w = st.sidebar.slider("氣氛", 0, 10, 5)
        price_w = st.sidebar.slider("價格", 0, 10, 5)
        green_w = st.sidebar.slider("綠色", 0, 10, 5)
        geo_w = st.sidebar.slider("地理位置", 0, 10, 5)
        overall_w = 0

    st.sidebar.header("地理位置")
    st.sidebar.write('請點選「取得我的位置」來獲取您的定位，以方便進行餐廳推薦；若未取得定位，系統將無法考慮此一構面。')

    return {
        "food_w": food_w,
        "service_w": service_w,
        "atmosphere_w": atmosphere_w,
        "price_w": price_w,
        "green_w": green_w,
        "geo_w": geo_w,
        "overall_w": overall_w,
    }


def render_question_block(section_title, questions, key_prefix):
    st.markdown(f'<div class="survey-section-title">{section_title}</div>', unsafe_allow_html=True)
    for qid, qtext in questions:
        st.markdown(f'<div class="survey-card"><strong>{qid}</strong>　{qtext}</div>', unsafe_allow_html=True)
        answer = st.radio(
            label="",
            options=[1, 2, 3, 4, 5],
            index=None,
            horizontal=True,
            key=f"{key_prefix}_{qid}",
            label_visibility="collapsed",
        )
        st.caption("1 = 非常不同意　2 = 不同意　3 = 普通　4 = 同意　5 = 非常同意")


def render_survey_page():
    if "latest_snapshot" not in st.session_state:
        st.warning("請先完成推薦系統體驗，再進入問卷填寫。")
        if st.button("返回推薦頁", use_container_width=True):
            go_to_page("recommend")
        return

    snapshot = st.session_state["latest_snapshot"]

    st.title("系統使用後問卷")
    st.markdown(
        """
        請根據你剛才操作本系統的實際感受作答。<br>
        本問卷採用 5 點李克特量表，1 代表「非常不同意」，5 代表「非常同意」。
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        f"""
        <div class="summary-chip">測試版本：系統 {snapshot['system_mode']}</div>
        <div class="summary-chip">是否使用定位：{'是' if snapshot['location_used'] else '否'}</div>
        <div class="summary-chip">已記錄本次 Top 10 推薦結果</div>
        """,
        unsafe_allow_html=True,
    )

    top3_names = [item["name"] for item in snapshot["top10"][:3]]
    if top3_names:
        st.info("你剛才看到的前 3 名推薦為：" + "、".join(top3_names))

    us_questions = [
        ("US1", "我對此餐廳推薦系統的整體使用經驗感到滿意。"),
        ("US2", "我認為此餐廳推薦系統所提供的推薦結果符合我的需求。"),
        ("US3", "整體而言，我對此餐廳推薦系統的評價是正面的。"),
    ]
    pu_questions = [
        ("PU1", "此餐廳推薦系統能幫助我更容易做出餐廳選擇。"),
        ("PU2", "此餐廳推薦系統能幫助我更有效率地比較不同餐廳資訊。"),
        ("PU3", "使用此餐廳推薦系統能節省我搜尋餐廳的時間。"),
        ("PU4", "整體而言，我認為此餐廳推薦系統對我選擇餐廳是有用的。"),
    ]
    tr_questions = [
        ("TR1", "我相信此餐廳推薦系統提供的推薦是可信的。"),
        ("TR2", "我相信此餐廳推薦系統能依據我的偏好推薦合適的餐廳。"),
        ("TR3", "若我要實際選擇餐廳，我願意把此餐廳推薦系統的推薦作為重要參考。"),
    ]

    with st.form("questionnaire_form"):
        render_question_block("一、使用者滿意度", us_questions, "us")
        render_question_block("二、知覺有用性", pu_questions, "pu")
        render_question_block("三、推薦信任", tr_questions, "tr")

        feedback_text = st.text_area(
            "其他意見（選填）",
            placeholder="例如：哪些地方最好用？哪些地方還可以再改進？",
            height=120,
        )
        consent = st.checkbox("我已完成系統體驗，並同意將本次匿名作答結果用於學術研究分析。")
        submitted = st.form_submit_button("提交問卷", use_container_width=True, type="primary")

    if submitted:
        answers = {
            "US1": st.session_state.get("us_US1"),
            "US2": st.session_state.get("us_US2"),
            "US3": st.session_state.get("us_US3"),
            "PU1": st.session_state.get("pu_PU1"),
            "PU2": st.session_state.get("pu_PU2"),
            "PU3": st.session_state.get("pu_PU3"),
            "PU4": st.session_state.get("pu_PU4"),
            "TR1": st.session_state.get("tr_TR1"),
            "TR2": st.session_state.get("tr_TR2"),
            "TR3": st.session_state.get("tr_TR3"),
        }

        unanswered = [qid for qid, value in answers.items() if value is None]
        if unanswered:
            st.warning("你還有題目尚未作答，請完成全部題目後再提交。")
        elif not consent:
            st.warning("請先勾選同意聲明後再提交。")
        else:
            response_uuid = save_questionnaire_response(snapshot, answers, feedback_text)
            st.session_state["last_response_uuid"] = response_uuid
            go_to_page("thank_you")

    col_left, col_right = st.columns(2)
    with col_left:
        if st.button("返回推薦頁", use_container_width=True):
            go_to_page("recommend")


def render_thank_you_page():
    st.success("問卷已成功送出，謝謝你的參與。")
    response_uuid = st.session_state.get("last_response_uuid", "-")
    st.write(f"本次回覆編號：`{response_uuid}`")
    st.caption("建議你保留這個編號，方便之後核對資料。")

    if st.button("返回首頁", use_container_width=True):
        for key in [
            "latest_snapshot",
            "last_response_uuid",
            "request_location",
            "location_status",
            "user_lat",
            "user_lon",
        ]:
            if key == "location_status":
                st.session_state[key] = "unknown"
            elif key in ["user_lat", "user_lon"]:
                st.session_state[key] = None
            else:
                st.session_state.pop(key, None)
        go_to_page("intro")


# =========================
# 13. 頁面初始化
# =========================
st.set_page_config(page_title="綠色餐廳推薦系統", layout="wide")
inject_global_styles()
ensure_response_table_exists()

# 版本控制：
# 1. 若網址有 ?mode=A / ?mode=B / ?mode=C，優先使用網址參數
# 2. 若網址沒帶參數，才使用預設版本 default_mode
# 3. 頁面切換則使用 ?page=intro / ?page=recommend / ?page=survey / ?page=thank_you
default_mode = "C"

try:
    mode_from_query = str(st.query_params.get("mode", default_mode)).upper()
    if mode_from_query not in {"A", "B", "C"}:
        mode_from_query = default_mode
except Exception:
    mode_from_query = default_mode

page_from_query = get_page_from_query("intro")

if "page" not in st.session_state:
    st.session_state["page"] = page_from_query

if "user_lat" not in st.session_state:
    st.session_state["user_lat"] = None

if "user_lon" not in st.session_state:
    st.session_state["user_lon"] = None

if "location_status" not in st.session_state:
    st.session_state["location_status"] = "unknown"

if "request_location" not in st.session_state:
    st.session_state["request_location"] = False

# 每次進入頁面都同步目前網址的 mode 與 page
st.session_state["system_mode"] = mode_from_query
st.session_state["page"] = page_from_query

route_changed = sync_query_route(mode_from_query, page_from_query)
if route_changed:
    st.rerun()

# 讀取資料
df = load_recommendation_base()


# =========================
# 14. 首頁
# =========================
if st.session_state["page"] == "intro":
    render_intro_page(df)


# =========================
# 15. 推薦頁
# =========================
elif st.session_state["page"] == "recommend":
    system_mode = st.session_state["system_mode"]
    weights = render_sidebar(system_mode)

    st.title("綠色餐廳推薦系統")
    st.write("請依照你的偏好調整各構面權重，系統將提供推薦結果。")
    st.info(f"目前測試版本：系統 {system_mode}")

    col_top_1, col_top_2 = st.columns([1, 1])

    with col_top_1:
        if st.button("返回首頁", use_container_width=True):
            go_to_page("intro")

    with col_top_2:
        if st.button("取得我的位置", use_container_width=True):
            st.session_state["request_location"] = True
            st.session_state["location_status"] = "unknown"
            st.rerun()

    location = None
    if st.session_state["request_location"]:
        location = streamlit_js_eval(
            js_expressions="""
            new Promise((resolve) => {
                if (!navigator.geolocation) {
                    resolve({status: "failed", message: "瀏覽器不支援定位"});
                } else {
                    navigator.geolocation.getCurrentPosition(
                        (position) => {
                            resolve({
                                status: "success",
                                lat: position.coords.latitude,
                                lon: position.coords.longitude
                            });
                        },
                        (error) => {
                            resolve({
                                status: "failed",
                                message: error.message
                            });
                        },
                        {
                            enableHighAccuracy: true,
                            timeout: 10000,
                            maximumAge: 0
                        }
                    );
                }
            })
            """,
            key="get_user_location"
        )

    if isinstance(location, dict):
        if location.get("status") == "success":
            st.session_state["user_lat"] = location.get("lat")
            st.session_state["user_lon"] = location.get("lon")
            st.session_state["location_status"] = "success"
            st.session_state["request_location"] = False
        elif location.get("status") == "failed":
            st.session_state["location_status"] = "failed"
            st.session_state["request_location"] = False

    if st.session_state["location_status"] == "success":
        st.success(
            f"已成功取得目前位置：緯度 {st.session_state['user_lat']:.6f}，經度 {st.session_state['user_lon']:.6f}"
        )
    elif st.session_state["location_status"] == "failed":
        st.warning("目前無法取得你的位置，系統將先忽略地理位置構面。")
    else:
        st.info("你可以按下「取得我的位置」來啟用地理位置功能。")

    use_geo = st.session_state["location_status"] == "success"

    ranked_df = calculate_recommendation_score(
        df=df,
        system_mode=system_mode,
        geo_w=weights["geo_w"],
        use_geo=use_geo,
        user_lat=st.session_state["user_lat"],
        user_lon=st.session_state["user_lon"],
        food_w=weights["food_w"],
        service_w=weights["service_w"],
        atmosphere_w=weights["atmosphere_w"],
        price_w=weights["price_w"],
        green_w=weights["green_w"],
        overall_w=weights["overall_w"],
    )

    snapshot = build_experiment_snapshot(
        ranked_df=ranked_df,
        system_mode=system_mode,
        weights=weights,
        use_geo=use_geo,
        user_lat=st.session_state["user_lat"],
        user_lon=st.session_state["user_lon"],
    )
    st.session_state["latest_snapshot"] = snapshot

    st.subheader("Top 10 推薦結果")
    top10_display = build_top10_display(ranked_df, system_mode)
    render_static_recommendation_table(top10_display)

    st.divider()
    st.markdown(
        """
        <div class="hero-card" style="padding: 22px 26px; margin-top: 1rem;">
            <div class="hero-title" style="font-size:1.35rem; margin-bottom:0.35rem;">完成推薦體驗後，請繼續填寫問卷</div>
            <div class="hero-subtitle" style="font-size:1rem;">
                系統會保留你本次操作的推薦版本、權重設定與 Top 10 推薦結果，
                讓問卷結果能與實際測試情境對應。
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.button("我已完成推薦體驗，前往填寫問卷", use_container_width=True, type="primary"):
        go_to_page("survey")


# =========================
# 16. 問卷頁
# =========================
elif st.session_state["page"] == "survey":
    render_survey_page()


# =========================
# 17. 完成頁
# =========================
elif st.session_state["page"] == "thank_you":
    render_thank_you_page()
