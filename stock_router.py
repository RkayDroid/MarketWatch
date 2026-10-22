from fastapi import APIRouter, HTTPException
import yfinance as yf

router = APIRouter(prefix="/stock", tags=["Stock Data"])


@router.get("/{ticker}")
def get_stock_info(ticker: str):
    try:
        stock = yf.Ticker(ticker.upper())
        info = stock.info
        if not info:
            raise HTTPException(status_code=404, detail="Ticker not found.")
        return {"ticker": ticker.upper(), "info": info}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{ticker}/history")
def get_stock_history(ticker: str, period: str = "1mo"):
    try:
        stock = yf.Ticker(ticker.upper())
        df = stock.history(period=period)
        if df.empty:
            raise HTTPException(status_code=404, detail="No historical data found for the given parameters.")

        data = df.reset_index()
        data["Date"] = data["Date"].astype(str)
        records = data.to_dict(orient="records")

        return {"ticker": ticker.upper(), "period": period, "history": records}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))