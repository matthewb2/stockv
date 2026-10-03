# -*- coding: utf-8 -*-
"""
로컬 DB에 저장된 3분봉 데이터를 조회하고 분석하는 파이썬 프로그램.
"""
import argparse
import sys
import pandas as pd

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
    print(f"   - 대상 DB 파일: {store_file}")

    df = load_ticker_3m_from_db(store_file, ticker)

    if df.empty:
        print(f"{Color.YELLOW}⚠️ [{ticker}] 저장된 3분봉 데이터가 없습니다.{Color.RESET}")
        return

    print(f"{Color.GREEN}✅ 총 {len(df)}개의 봉 데이터가 로드되었습니다.{Color.RESET}")
    
    # 데이터 상/하단 미리보기 출력
    print("\n--- [최근 200개 3분봉] ---")
    print(df.tail(200).to_string())

    # 기본 통계 정보 출력
    if 'close' in df.columns:
        latest_close = df['close'].iloc[-1]
        max_high = df['high'].max() if 'high' in df.columns else 0
        min_low = df['low'].min() if 'low' in df.columns else 0
        print(f"\n📈 최신 종가: {latest_close:,.0f}원")
        print(f"📊 조회 기간 내 최고가: {max_high:,.0f}원 / 최저가: {min_low:,.0f}원")


def parse_args():
    parser = argparse.ArgumentParser(description="로컬 DB 3분봉 데이터 조회 도구")
    parser.add_argument("--ticker", required=True, help="조회할 종목코드 (예: 005930)")
    parser.add_argument("--store-file", default="kospi_chart.db", help="3분봉 누적 DB 경로 (기본: kospi_chart.db)")
    return parser.parse_args()

#python load_db_data.py --ticker 005930 --store-file kospi_chart.db
if __name__ == "__main__":
    #args = parse_args()
    #inspect_ticker_data(args.store_file, args.ticker)
    inspect_ticker_data("kospi_chart.db", "005930")