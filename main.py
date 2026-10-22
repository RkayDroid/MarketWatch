from datetime import datetime
from fastapi import FastAPI
from stock_router import router as stock_router
import pytz

app = FastAPI(title="Timezone API", version="1.0.0")
# Include the stock router
app.include_router(stock_router)
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
