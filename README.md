# 📊 Real-Time Earnings Monitoring Data Pipeline

An automated financial data pipeline that consolidates earnings, financial fundamentals, market data, news activity, analyst price-target changes, and company information from multiple external data sources into a single analysis-ready dataset.

The pipeline is designed to run as a **Google Cloud Run Job**, retrieve credentials securely from environment variables / Secret Manager, and automatically publish the resulting dataset to **Google Cloud Storage**.

---

## 🚀 Project Overview

The **Real-Time Earnings Monitoring Data Pipeline** was built to automate the collection and enrichment of publicly available market and earnings information for companies reporting earnings.

Instead of relying on a single API, the pipeline combines data from multiple sources to create a richer dataset containing **50+ fields per ticker**.

The final dataset can be used for:

- 📈 Earnings analysis
- 🔎 Pre-earnings screening
- 📊 Quantitative research
- 🤖 Machine learning feature engineering
- 💹 Trading strategy research
- 📰 Market sentiment and news analysis
- 🏢 Company fundamental analysis

---

## 🏗️ Architecture

                    ┌─────────────────────┐
                    │   Cloud Run Job     │
                    │   Python Pipeline   │
                    └──────────┬──────────┘
                               │
          ┌────────────────────┼────────────────────┐
          │                    │                    │
          ▼                    ▼                    ▼
     ┌─────────┐          ┌─────────┐          ┌──────────┐
     │ Finnhub │          │ Polygon │          │ Alpaca   │
     └────┬────┘          └────┬────┘          └────┬─────┘
          │                    │                    │
          │                    ▼                    │
          │               yFinance                 │
          │                    │                    │
          ▼                    ▼                    ▼
     Earnings             Financials            Market Data
     EPS History          Revenue               Price Changes
     News Count           Fundamentals           Volume
          │                    │                    │
          └──────────────┬─────┴────────────────────┘
                         │
                         ▼
                 ┌─────────────────┐
                 │   The Fly       │
                 │ Price Target    │
                 │ Hikes / News    │
                 └────────┬────────┘
                          │
                          ▼
                 ┌─────────────────┐
                 │ EarningsWhispers│
                 │ Calendar Data   │
                 └────────┬────────┘
                          │
                          ▼
                 ┌─────────────────┐
                 │ Merged Dataset  │
                 │    50+ Fields   │
                 └────────┬────────┘
                          │
                          ▼
                 ┌─────────────────┐
                 │ Google Cloud    │
                 │    Storage      │
                 │     CSV         │
                 └─────────────────┘
