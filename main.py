from fastapi import FastAPI
from stock_router import router as stock_router, upstream_budget
from user_router import router as user_router
from watchlist_router import router as watchlist_router

app = FastAPI(
    title="Market Watch Lite API",
    version="3.3.0",
    description=(
        "Stock snapshot and OHLC price history backed by yfinance, plus registered users and their "
        "watchlists (in-memory). Rate limited to stay inside Yahoo Finance's free-tier budget. No caching (yet)."
    ),
)
app.include_router(stock_router)
app.include_router(user_router)
app.include_router(watchlist_router)


@app.get("/status")
def get_status():
    return {"status": "online", "message": "API is running successfully", "upstream_budget": upstream_budget()}
