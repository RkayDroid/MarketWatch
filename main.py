from fastapi import FastAPI
from stock_router import router as stock_router, upstream_budget
from user_router import router as user_router
from watchlist_router import router as watchlist_router
from fastapi.middleware.cors import CORSMiddleware
app = FastAPI(
    title="Market Watch Lite API",
    version="3.3.0",
    description=(
        "Stock snapshot and OHLC price history backed by yfinance, plus registered users and their "
        "watchlists (in-memory). Rate limited to stay inside Yahoo Finance's free-tier budget. No caching (yet)."
    ),
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173",
                   "http://localhost:5174",
                   "http://localhost:5175",
                   "http://localhost:5176" ],  # Match your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(stock_router)
app.include_router(user_router)
app.include_router(watchlist_router)


@app.get("/status")
def get_status():
    return {"status": "online", "message": "API is running successfully", "upstream_budget": upstream_budget()}
