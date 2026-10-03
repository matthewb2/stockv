# -*- coding: utf-8 -*-
"""
3분봉 조회/정규화.

KIS 3분봉 응답을 학습에 쓸 수 있는 정방향(과거→최신) DataFrame 으로
정리하는 responsibilities만 모아둔 모듈이다. (저장은 chart_store 가 담당)

kis 객체는 인자로만 받으므로 KISNative 을 import 하지 않아도 되어
단독 테스트가 가능하다.
"""
import pandas as pd

from scripts.console_color import Color

OHLCV_COLUMNS = ('open', 'high', 'low', 'close', 'volume')


def fetch_ticker_3m_data(kis, ticker):
    """KIS 3분봉 조회 → 정방향 DataFrame(과거→최신)"""
    try:
        response = kis.get_3m_chart(ticker)
        if not response:
            print(f"{Color.YELLOW}⚠️ [{ticker}] 3분봉 데이터가 없습니다.{Color.RESET}")
            return pd.DataFrame()

        # API는 최신순(행0=현재 봉)으로 내려준다 → 시간 오름차순으로 뒤집기
        df = pd.DataFrame(response).iloc[::-1].reset_index(drop=True)

        for col in OHLCV_COLUMNS:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        df = df.dropna(subset=['close', 'volume'])
        return df.sort_values('time').reset_index(drop=True)
    except Exception as e:
        print(f"{Color.RED}⚠️ [{ticker}] 3분봉 조회 실패: {e}{Color.RESET}")
        return pd.DataFrame()