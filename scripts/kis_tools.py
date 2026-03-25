# -*- coding: utf-8 -*-
import os
from pykis import PyKis
from dotenv import load_dotenv
from time import sleep 

load_dotenv()

class KISTools:
    def __init__(self):
        # PyKis 인스턴스 생성 (사용자 제공 공식 예제 규격)
        self.api = PyKis(
            id=os.getenv('KIS_ID'),
            account=os.getenv('KIS_VIRTUAL_ACCOUNT'),
            appkey=os.getenv('KIS_APPKEY'),
            secretkey=os.getenv('KIS_SECRETKEY'),
            virtual_id=os.getenv('KIS_VIRTUAL_ID'),
            virtual_appkey=os.getenv('KIS_VIRTUAL_APPKEY'),
            virtual_secretkey=os.getenv('KIS_VIRTUAL_SECRETKEY'),
            keep_token=True
        )
        
        # [수정] pykis는 초기화 시 입력된 정보를 바탕으로 self.api.account에 
        # 적절한 계좌 객체를 자동으로 할당합니다.
        self.account = self.api.account()

    def get_market_data(self, code, timeframe="D"):
        """시세 및 차트 조회"""
        stock = self.api.stock(code)
        quote = stock.quote()
        sleep(1) # API 호출 직후 지연
        current_price = quote.price
        
        period = "d" if timeframe == "D" else "m"
        chart = stock.chart(period=period)
        bars = list(chart)
        
        if not bars:
            raise ValueError(f"{code}의 차트 데이터를 불러오지 못했습니다.")

        recent = bars[-30:]
        
        return {
            "price": float(current_price),
            "closes": [float(b.close) for b in recent],
            "volumes": [float(b.volume) for b in recent]
        }

    def order(self, code, qty, side="buy"):
        """
        공식 위키 권장 방식: stock 객체를 생성하여 주문 수행
        예시: stock = api.stock("005930"); stock.buy(qty=10)
        """
        if not self.account:
            raise ValueError("계좌 객체가 활성화되지 않았습니다.")
            
        # 1. 종목 객체 생성 (stock 함수 이용)
        stock = self.api.stock(code)
        sleep(1) # API 호출 직후 지연
        try:
            if side.lower() == "buy":
                # 2. stock 객체의 buy 메서드 호출 (시장가 주문)
                # 공식 위키에 따라 qty 인자를 명시적으로 전달합니다.
                return stock.buy(qty=qty, condition=None, execution=None)
            else:
                # 2. stock 객체의 sell 메서드 호출 (시장가 주문)
                return stock.sell(qty=qty, condition=None, execution=None) 
        except Exception as e:
            print(f"주문 실행 중 오류 발생: {e}")
            raise e