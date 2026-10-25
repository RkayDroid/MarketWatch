from datetime import datetime
from fastapi import FastAPI
from stock_router import cache_router, router as stock_router
import pytz

app = FastAPI(
    title="Market Watch Lite API",
    version="1.1.0",
    description=(
        "Stock snapshot, price history and high/low data backed by yfinance, "
        "with caching and rate limiting to stay inside Yahoo Finance's free-tier budget."
    ),
)
app.include_router(stock_router)
app.include_router(cache_router)
MAJOR_TIMEZONES = [
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "Europe/London",
    "Europe/Paris",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Kolkata",
    "Australia/Sydney"
]

@app.get("/time")
def get_all_times():
    #timezones = pytz.all_timezones
    current_times = {}

    for tz_name in MAJOR_TIMEZONES:
        try:
            tz = pytz.timezone(tz_name)
            current_times[tz_name] = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z%z")
        except Exception:
            continue

    return {"current_times": current_times}

@app.get("/status")
def get_status():
    return {"status": "online", "message": "API is running successfully"}
