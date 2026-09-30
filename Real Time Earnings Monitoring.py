# -*- coding: utf-8 -*-
"""
Created on Thu Oct  1 09:36:27 2025

@author: roberto espinoza
"""

"""
Real-time earnings monitoring dataset - Cloud Run Job version.

Same logic as the notebook (sections a-g + merge + save), restructured as a
script that:
  - reads API keys from environment variables (populated from Secret Manager)
  - writes the final CSV to a Cloud Storage bucket instead of local disk

Env vars expected at runtime:
  FINNHUB_API_KEY, POLYGON_API_KEY, ALPACA_API_KEY, ALPACA_SECRET_KEY
  GCS_BUCKET            - bucket name to write the output CSV to
  GCS_PREFIX (optional) - folder prefix inside the bucket, default "earnings"
"""

import os
import re
import time
from datetime import date, datetime, timedelta

import requests
import pandas as pd
import finnhub
import yfinance as yf
from bs4 import BeautifulSoup
from google.cloud import storage

# ============================================================
# Config
# ============================================================
FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
POLYGON_API_KEY = os.environ["POLYGON_API_KEY"]
ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]

GCS_BUCKET = os.environ["GCS_BUCKET"]
GCS_PREFIX = os.environ.get("GCS_PREFIX", "earnings")

finnhub_client = finnhub.Client(api_key=FINNHUB_API_KEY)
TODAY = datetime.now().strftime("%Y-%m-%d")


# ============================================================
# a. Earnings calendar (Finnhub)
# ============================================================
def get_earnings_calendar(date_from: str, date_to: str, api_key: str = FINNHUB_API_KEY) -> pd.DataFrame:
    url = "https://finnhub.io/api/v1/calendar/earnings"
    params = {"from": date_from, "to": date_to, "token": api_key}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    rows = []
    for item in data.get("earningsCalendar", []):
        rows.append({
            "Date": item.get("date"),
            "Symbol": item.get("symbol"),
            "AH/PM": item.get("hour"),
            "Quarter": item.get("quarter"),
            "Year": item.get("year"),
            "Estimated Earnings": item.get("epsEstimate"),
            "Estimated Revenue": item.get("revenueEstimate"),
        })
    return pd.DataFrame(rows)


# ============================================================
# b. Quarterly EPS history (Finnhub)
# ============================================================
def get_quarterly_eps(ticker: str, api_key: str = FINNHUB_API_KEY, client=None) -> dict:
    client = client or finnhub.Client(api_key=api_key)
    records = client.company_earnings(ticker, limit=6)
    records = sorted(records, key=lambda r: r.get("period", ""), reverse=True)[:4]

    result = {}
    for i, rec in enumerate(records, start=1):
        result[f"eps t-{i}q period"] = rec.get("period")
        result[f"eps t-{i}q actual"] = rec.get("actual")
        result[f"eps t-{i}q estimate"] = rec.get("estimate")
    for i in range(len(records) + 1, 5):
        result[f"eps t-{i}q period"] = None
        result[f"eps t-{i}q actual"] = None
        result[f"eps t-{i}q estimate"] = None
    return result


# ============================================================
# c. Quarterly revenue (Polygon -> yfinance fallback)
# ============================================================
def get_quarterly_revenue_polygon(ticker: str, api_key: str = POLYGON_API_KEY, limit: int = 8):
    url = "https://api.polygon.io/vX/reference/financials"
    params = {"ticker": ticker, "timeframe": "quarterly", "limit": limit, "apiKey": api_key}
    resp = requests.get(url, params=params, timeout=15)
    data = resp.json()
    if "results" not in data or not data["results"]:
        return None
    results = []
    for filing in data["results"]:
        income = filing.get("financials", {}).get("income_statement", {})
        revenue = income.get("revenues", {}).get("value")
        results.append({"end_date": filing.get("end_date"), "revenue": revenue})
    return results


def get_quarterly_revenue_yfinance(ticker: str):
    stock = yf.Ticker(ticker)
    q_financials = stock.quarterly_financials
    if q_financials is None or q_financials.empty or "Total Revenue" not in q_financials.index:
        return None
    series = q_financials.loc["Total Revenue"]
    return [{"end_date": dt.strftime("%Y-%m-%d"), "revenue": val} for dt, val in series.items()]


def get_quarterly_revenue(ticker: str, polygon_api_key: str = POLYGON_API_KEY) -> dict:
    results = get_quarterly_revenue_polygon(ticker, polygon_api_key)
    source = "polygon"
    if not results:
        results = get_quarterly_revenue_yfinance(ticker)
        source = "yfinance"
        if not results:
            print(f"  [{ticker}] revenue: no data from Polygon or yfinance")

    result = {}
    if results:
        results = results[:4]
        for i, rec in enumerate(results, start=1):
            result[f"revenue t-{i}q period"] = rec.get("end_date")
            result[f"revenue t-{i}q actual"] = rec.get("revenue")
        for i in range(len(results) + 1, 5):
            result[f"revenue t-{i}q period"] = None
            result[f"revenue t-{i}q actual"] = None
    else:
        for i in range(1, 5):
            result[f"revenue t-{i}q period"] = None
            result[f"revenue t-{i}q actual"] = None
    result["revenue_source"] = source
    return result


# ============================================================
# d. Price info (yfinance gap + Alpaca % change)
# ============================================================
def get_price_gap(ticker: str) -> dict:
    try:
        data_5m = yf.download(ticker, period="1d", interval="5m", prepost=True, progress=False)
        data_close = yf.download(ticker, period="5d", interval="1d", prepost=True, progress=False)
        if data_5m.empty or data_close.empty:
            return {"GAP": None}
        latest_price_exthours = data_5m["Close"].iloc[-1]
        latest_price_daily = data_close["Close"].iloc[-1]
        if hasattr(latest_price_exthours, "item"):
            latest_price_exthours = latest_price_exthours.item()
        if hasattr(latest_price_daily, "item"):
            latest_price_daily = latest_price_daily.item()
        return {"GAP": (latest_price_exthours - latest_price_daily) / latest_price_daily}
    except Exception as e:
        print(f"  [{ticker}] GAP error: {e}")
        return {"GAP": None}


def get_price_changes_alpaca(ticker: str, api_key: str = ALPACA_API_KEY, secret_key: str = ALPACA_SECRET_KEY) -> dict:
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
    windows = {
        "% last 5 days": 5, "% last 30 days": 30, "% last 90 days": 90,
        "% last 180 days": 180, "% last year": 365, "% last two years": 730, "% last 3 years": 1095,
    }
    end = datetime.now()
    start = end - timedelta(days=max(windows.values()) + 10)
    url = f"https://data.alpaca.markets/v2/stocks/{ticker}/bars"
    params = {
        "timeframe": "1Day",
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 10000,
        "adjustment": "split",
    }
    result = {label: None for label in windows}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        bars = resp.json().get("bars", [])
        if not bars:
            return result
        closes = sorted(
            [(datetime.strptime(b["t"][:10], "%Y-%m-%d"), b["c"]) for b in bars], key=lambda x: x[0]
        )
        latest_date, latest_close = closes[-1]
        for label, days in windows.items():
            target_date = latest_date - timedelta(days=days)
            past = [c for d, c in closes if d <= target_date]
            if past:
                result[label] = (latest_close - past[-1]) / past[-1]
    except Exception as e:
        print(f"  [{ticker}] Alpaca price-change error: {e}")
    return result


def get_price_info(ticker: str) -> dict:
    info = {}
    info.update(get_price_gap(ticker))
    info.update(get_price_changes_alpaca(ticker))
    return info


# ============================================================
# e. Volume (yfinance)
# ============================================================
def get_volume_info(ticker: str) -> dict:
    info = yf.Ticker(ticker).info
    return {
        "10 day average volume": info.get("averageVolume10days"),
        "3 months average volume": info.get("averageVolume"),
    }


# ============================================================
# f. News count + price target hikes
# ============================================================
def get_news_count_last_month(ticker: str, api_key: str = FINNHUB_API_KEY, client=None) -> dict:
    client = client or finnhub.Client(api_key=api_key)
    today_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    try:
        news = client.company_news(ticker, _from=start_date, to=today_date)
        return {"Number of news last month": len(news)}
    except Exception as e:
        print(f"  [{ticker}] news count error: {e}")
        return {"Number of news last month": None}


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
PT_RAISE_RE = re.compile(r"price target raised.*?\bat\s+([A-Z][A-Za-z0-9&.\-\s]+?)(?:,|$)", re.IGNORECASE)


def parse_relative_date(label: str, today: datetime):
    label = label.strip().lower()
    if label.startswith("today"):
        return today
    if label.startswith("yesterday"):
        return today - timedelta(days=1)
    for i, wd in enumerate(WEEKDAYS):
        if label.startswith(wd):
            days_back = (today.weekday() - i) % 7
            days_back = days_back if days_back != 0 else 7
            return today - timedelta(days=days_back)
    for fmt in ("%b %d", "%B %d"):
        try:
            dt = datetime.strptime(label.title(), fmt).replace(year=today.year)
            if dt > today:
                dt = dt.replace(year=today.year - 1)
            return dt
        except ValueError:
            continue
    return None


def fetch_news_items(ticker: str):
    url = f"https://www.thefly.com/{ticker.upper()}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    items = []
    current_date_label = "Today"
    today = datetime.now()
    text_blocks = [t.strip() for t in soup.get_text("\n").split("\n") if t.strip()]
    day_separators = {"today", "yesterday"} | set(WEEKDAYS)

    i = 0
    while i < len(text_blocks):
        line = text_blocks[i]
        low = line.lower()
        if low in day_separators:
            current_date_label = line
            i += 1
            continue
        if "price target" in low:
            timestamp = None
            for j in range(i + 1, min(i + 6, len(text_blocks))):
                if re.match(r"^\d{1,2}:\d{2}\s*(AM|PM)$", text_blocks[j], re.IGNORECASE):
                    timestamp = text_blocks[j]
                    break
            items.append({"headline": line, "date_label": current_date_label, "time": timestamp})
        i += 1
    return items


def count_price_target_hikes(ticker: str, earnings_date: str) -> dict:
    try:
        earnings_dt = datetime.strptime(earnings_date, "%Y-%m-%d")
        today = datetime.now()
        items = fetch_news_items(ticker)
        hikes = []
        for item in items:
            match = PT_RAISE_RE.search(item["headline"])
            if not match:
                continue
            item_date = parse_relative_date(item["date_label"], today)
            if item_date is None:
                continue
            if item_date.date() >= earnings_dt.date():
                hikes.append({
                    "firm": match.group(1).strip(),
                    "headline": item["headline"],
                    "date": item_date.strftime("%Y-%m-%d"),
                    "time": item["time"],
                })
        return {"Target Price Hikes": len(hikes), "Target Price Hikes Detail": hikes}
    except Exception as e:
        print(f"  [{ticker}] price target hike error: {e}")
        return {"Target Price Hikes": None, "Target Price Hikes Detail": []}


def get_news_info(ticker: str, earnings_date: str) -> dict:
    info = {}
    info.update(get_news_count_last_month(ticker))
    info.update(count_price_target_hikes(ticker, earnings_date))
    return info


# ============================================================
# g. Company info (yfinance)
# ============================================================
def format_pct(value):
    return f"{value * 100:.2f}%" if isinstance(value, (int, float)) else value


def get_ipo_date(ticker: yf.Ticker) -> str:
    try:
        hist = ticker.history(period="max")
        if not hist.empty:
            return hist.index[0].strftime("%Y-%m-%d")
    except Exception:
        pass
    return "N/A"


def get_institutions_holding_count(ticker: yf.Ticker):
    try:
        mh = ticker.major_holders
        if mh is None or mh.empty:
            return "N/A"
        if "institutionsCount" in mh.index:
            return mh.loc["institutionsCount"].values[0]
        for col in mh.columns:
            match = mh[mh[col].astype(str).str.contains(
                "Number of Institutions Holding Shares", case=False, na=False
            )]
            if not match.empty:
                other_col = [c for c in mh.columns if c != col][0]
                return match[other_col].values[0]
        return "N/A"
    except Exception:
        return "N/A"


def get_company_info(symbol: str) -> dict:
    ticker = yf.Ticker(symbol)
    info = ticker.info
    return {
        "Company Name": info.get("longName"),
        "Market Cap": info.get("marketCap"),
        "Float Shares": info.get("floatShares"),
        "IPO Date (approx.)": get_ipo_date(ticker),
        "Sector": info.get("sector"),
        "Industry": info.get("industry"),
        "Short Ratio": info.get("shortRatio"),
        "Shares Short": info.get("sharesShort"),
        "Short % of Float": format_pct(info.get("shortPercentOfFloat")),
        "Website": info.get("website"),
        "Number of Institutions Holding Shares": get_institutions_holding_count(ticker),
        "% Held by Insiders": format_pct(info.get("heldPercentInsiders")),
        "% Held by Institutions": format_pct(info.get("heldPercentInstitutions")),
        "Company Description": info.get("longBusinessSummary"),
    }


# ============================================================
# EarningsWhispers merge
# ============================================================
def get_earnings_calendar_whispers(day: date) -> pd.DataFrame:
    d = day.strftime("%Y%m%d")
    url = f"https://www.earningswhispers.com/api/caldata/{d}"
    headers = {
        "User-Agent": HEADERS["User-Agent"],
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.earningswhispers.com/calendar/{d}",
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return pd.DataFrame(resp.json())


# ============================================================
# Orchestrator
# ============================================================
def build_earnings_dataset(date_from: str = TODAY, date_to: str = TODAY, pause_sec: float = 0.0) -> pd.DataFrame:
    calendar_df = get_earnings_calendar(date_from, date_to)
    all_rows = []
    for _, row in calendar_df.iterrows():
        symbol = row["Symbol"]
        earnings_date = row["Date"]
        print(f"Processing {symbol}...")
        record = row.to_dict()

        for label, fn in [
            ("EPS", lambda: record.update(get_quarterly_eps(symbol))),
            ("Revenue", lambda: record.update(get_quarterly_revenue(symbol))),
            ("Price", lambda: record.update(get_price_info(symbol))),
            ("Volume", lambda: record.update(get_volume_info(symbol))),
            ("News", lambda: record.update(get_news_info(symbol, earnings_date))),
            ("Company info", lambda: record.update(get_company_info(symbol))),
        ]:
            try:
                fn()
            except Exception as e:
                print(f"  [{symbol}] {label} error: {e}")

        all_rows.append(record)
        if pause_sec:
            time.sleep(pause_sec)

    return pd.DataFrame(all_rows)


def upload_to_gcs(local_path: str, bucket_name: str, blob_name: str) -> str:
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(local_path)
    return f"gs://{bucket_name}/{blob_name}"


def main():
    earnings_dataset = build_earnings_dataset()

    whispers_df = get_earnings_calendar_whispers(date.today())
    whispers_df = whispers_df.rename(columns={"ticker": "Symbol"})
    earnings_dataset["Symbol"] = earnings_dataset["Symbol"].astype(str).str.strip().str.upper()
    whispers_df["Symbol"] = whispers_df["Symbol"].astype(str).str.strip().str.upper()
    merged_dataset = earnings_dataset.merge(whispers_df, on="Symbol", how="left")

    unmatched = merged_dataset[merged_dataset["company"].isna()]["Symbol"].tolist()
    if unmatched:
        print(f"{len(unmatched)} symbols had no match in Whispers data: {unmatched}")

    local_path = f"/tmp/earnings_dataset_{TODAY}.csv"
    merged_dataset.to_csv(local_path, index=False)

    gcs_path = upload_to_gcs(local_path, GCS_BUCKET, f"{GCS_PREFIX}/earnings_dataset_{TODAY}.csv")
    print(f"Saved {len(merged_dataset)} rows to {gcs_path}")


if __name__ == "__main__":
    main()