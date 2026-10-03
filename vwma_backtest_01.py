import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM
import yfinance as yf
import argparse
import sys

# Windows 콘솔(cp949)에서도 한글 출력이 가능하도록 설정
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from scripts.chart_store import open_store
from scripts.console_color import Color


def load_ticker_3m_from_db(store_file: str, ticker: str) -> pd.DataFrame:
    """로컬 저장소(ChartDB)에서 특정 종목의 3분봉 데이터를 불러옵니다.

    Args:
        store_file: DB 파일 경로 (예: kospi_chart.db)
        ticker: 종목코드 (예: 005930)

    Returns:
        pd.DataFrame: 3분봉 데이터프레임
    """
    store = open_store(store_file)
    try:
        # ChartDB 객체에서 해당 종목의 DataFrame을 가져옴
        df = store.get_df(ticker)
        return df
    finally:
        store.close()


def inspect_ticker_data(store_file: str, ticker: str):
    """종목의 로컬 3분봉 데이터를 조회하여 요약 정보를 출력합니다."""
    print(f"\n{Color.BOLD}🔎 [{ticker}] 로컬 DB 3분봉 데이터 조회{Color.RESET}")
    print(f"    - 대상 DB 파일: {store_file}")

    df = load_ticker_3m_from_db(store_file, ticker)

    if df is None or df.empty:
        print(f"{Color.YELLOW}⚠️ [{ticker}] 저장된 3분봉 데이터가 없습니다.{Color.RESET}")
        return None

    print(f"{Color.GREEN}✅ 총 {len(df)}개의 봉 데이터가 로드되었습니다.{Color.RESET}")
    return df


# VWMA 및 이격도 기반 피처 엔지니어링
def calculate_vwma(close, volume, window):
    return (close * volume).rolling(window=window).sum() / volume.rolling(window=window).sum()


def test():
    df = inspect_ticker_data("kospi_chart.db", "005930")

    if df is None or df.empty:
        return

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # 💡 컬럼 이름 대소문자 차이로 인한 KeyError 방지 (소문자/대문자 혼용 대응)
    df.columns = [str(col).strip().capitalize() for col in df.columns]

    df = df.dropna()

    df["VWMA_5"] = calculate_vwma(df["Close"], df["Volume"], 5)
    df["VWMA_20"] = calculate_vwma(df["Close"], df["Volume"], 20)
    df["VWMA_60"] = calculate_vwma(df["Close"], df["Volume"], 60)

    # 이격도 및 이격도의 변화량(모멘텀) 계산
    df["Divergence"] = (df["VWMA_5"] - df["VWMA_20"]) / df["VWMA_20"]
    df["Div_Momentum"] = df["Divergence"].diff()  # 💡 이격도의 속도(변화량) 피처 추가
    df["Feature2"] = (df["VWMA_20"] - df["VWMA_60"]) / df["VWMA_60"]

    df = df.dropna()

    # HMM 학습 피처 설정: 이격도 수준과 모멘텀(변화량) 조합
    hmm_features = ["Div_Momentum", "Feature2"]
    X = df[hmm_features].values

    # 3. Gaussian HMM 모델 학습 및 상태 매핑
    hmm_model = GaussianHMM(n_components=3, covariance_type="diag", n_iter=2000, random_state=42)
    hmm_model.fit(X)

    # 💡 상태 정렬 변경: 모멘텀(첫 번째 열) 평균값이 가장 큰 상태를 'Gold_Cross'로 정의
    sorted_states = np.argsort(hmm_model.means_[:, 0])  
    dead_state = sorted_states[0]      # 모멘텀이 가장 강한 하락 (데드크로스)
    neutral_state = sorted_states[1]   # 중립 (횡보)
    gold_state = sorted_states[2]      # 모멘텀이 가장 강한 상승 전환 (골드크로스 변곡점)

    state_mapping = {
        gold_state: 'Gold_Cross',
        neutral_state: 'Neutral',
        dead_state: 'Dead_Cross'
    }

    hidden_states = hmm_model.predict(X)
    df['State_Name'] = [state_mapping[s] for s in hidden_states]

    # 4. 백테스팅 시뮬레이션 및 개별 매매 손익률 기록
    position = 0  # 0: 현금 보유, 1: 주식 보유
    buy_price = 0.0
    buy_time = None
    trade_records = []  # 개별 매매 내역 저장 리스트

    for i in range(len(df)):
        current_price = df["Close"].iloc[i]
        state = df['State_Name'].iloc[i]
        timestamp = df.index[i]
        
        # Gold_Cross 국면 진입 시 매수
        if state == 'Gold_Cross' and position == 0:
            position = 1
            buy_price = current_price
            buy_time = timestamp
            
        # Dead_Cross 국면 진입 시 매도 (한 사이클 완성)
        elif state == 'Dead_Cross' and position == 1:
            position = 0
            sell_price = current_price
            sell_time = timestamp
            
            # 개별 매매 손익률 계산 (%)
            trade_return = ((sell_price - buy_price) / buy_price) * 100
            is_win = 1 if trade_return > 0 else 0
            
            trade_records.append({
                'Buy_Time': buy_time,
                'Buy_Price': buy_price,
                'Sell_Time': sell_time,
                'Sell_Price': sell_price,
                'Return_Pct': trade_return,
                'Win': is_win
            })

    # 5. 성과 지표 및 적중률(승률) 계산
    total_trades = len(trade_records)

    if total_trades > 0:
        winning_trades = sum(1 for t in trade_records if t['Win'] == 1)
        losing_trades = total_trades - winning_trades
        win_rate = (winning_trades / total_trades) * 100
        avg_return = np.mean([t['Return_Pct'] for t in trade_records])
        
        # 누적 수익률 계산
        cumulative_return_pct = (np.prod([1 + (t['Return_Pct'] / 100) for t in trade_records]) - 1) * 100
    else:
        winning_trades = 0
        losing_trades = 0
        win_rate = 0.0
        avg_return = 0.0
        cumulative_return_pct = 0.0

    # 6. 결과 출력
    print("\n" + "="*50)
    print(f" 📊 3분봉 HMM 국면 전환 전략 백테스팅 결과 (모멘텀 기반)")
    print("="*50)
    print(f"총 매매 횟수: {total_trades}회")
    if total_trades > 0:
        print(f"수익 횟수: {winning_trades}회")
        print(f"손실 횟수: {losing_trades}회")
        print(f"🎯 적중률 (승률): {win_rate:.2f}%")
        print(f"📈 평균 매매당 수익률: {avg_return:.2f}%")
        print(f"💰 누적 전략 수익률: {cumulative_return_pct:.2f}%")
    else:
        print("🎯 적중률 (승률): 0.00% (조건을 만족하는 매매 기록 없음)")

    print("\n--- 세부 매매 내역 (최근 5건) ---")
    if trade_records:
        trade_df = pd.DataFrame(trade_records)
        print(trade_df.tail(5).to_string(index=False))
    else:
        print("조건에 맞는 완결된 매매 내역이 없습니다.")


if __name__ == "__main__":
    test()