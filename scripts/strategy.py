import pandas as pd

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return 50.0
    delta = pd.Series(prices).diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    value = float(rsi.iloc[-1])
    # 봉이 평탄하거나 이동평균이 0인 경우 NaN이 나오므로 중립값으로 대체
    return 50.0 if pd.isna(value) else value