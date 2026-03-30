# -*- coding: utf-8 -*-
import os
import math
from pykis import PyKis
from dotenv import load_dotenv
from time import sleep 

load_dotenv()

class KISTools:
    _instance = None  # 싱글톤 인스턴스 저장용
    token = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(KISTools, cls).__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, 'initialized'): return
        self.initialized = True
        
        self.api = PyKis(
            id=os.getenv('KIS_VIRTUAL_ID'),
            #모의투자 계좌 사용
            account=os.getenv('KIS_VIRTUAL_ACCOUNT'),
            appkey=os.getenv('KIS_APPKEY'),
            secretkey=os.getenv('KIS_SECRETKEY'),
            virtual_id=os.getenv('KIS_VIRTUAL_ID'),
            virtual_appkey=os.getenv('KIS_VIRTUAL_APPKEY'),
            virtual_secretkey=os.getenv('KIS_VIRTUAL_SECRETKEY'),
            keep_token=True
        )
        self.token = self.api.token
        self.account = self.api.account()

    def get_deposit(self):
        """계좌의 예수금(주문 가능 금액) 조회"""
        if not self.account:
            return 0
        balance = self.account.balance()
        deposit = int(balance.pamt.dnca_tot_amt) 
        return deposit

    def get_market_data(self, code, timeframe="3"):
        """
        시세 및 차트 조회 (PyKis 공식 문서 기준 수정)
        timeframe: "D", "W", "M" 또는 분 단위 숫자 (기본값 "3")
        """
        stock = self.api.stock(code)
        
        # 1. 현재가 조회 (호가 정보)
        quote = stock.quote()
        sleep(1) 
        current_price = quote.price
        
        # 2. 차트 데이터 조회 (공식 문서 방식 적용)
        try:
            if timeframe == "D":
                chart = stock.chart()  # 기본값 일봉
            elif timeframe == "W":
                chart = stock.chart(period="week")
            elif timeframe == "M":
                chart = stock.chart(period="month")
            else:
                # timeframe이 숫자 형태("1", "3", "5")일 경우 당일 분봉 조회
                # 예: stock.chart(period=3) -> 당일 3분봉
                chart = stock.chart(period=int(timeframe))
            
            bars = list(chart)
        except Exception as e:
            print(f"⚠️ 차트 데이터 조회 중 오류: {e}")
            # 예외 발생 시 안전하게 일봉으로 폴백
            chart = stock.chart()
            bars = list(chart)
        
        if not bars:
            raise ValueError(f"{code}의 차트 데이터를 불러오지 못했습니다.")

        # 3. 최근 30개의 데이터 추출
        recent = bars[-30:]
        
        return {
            "price": float(current_price),
            "closes": [float(b.close) for b in recent],
            "volumes": [float(b.volume) for b in recent]
        }
    def buy_ten_percent(self, code):
        """매수 가능 금액의 10%만큼 해당 종목 매수"""
        try:
            stock = self.api.stock(code)
            orderable = stock.orderable_amount()
            total_orderable_amount = float(orderable.amount)
            target_budget = total_orderable_amount * 0.1
            current_unit_price = float(orderable.unit_price)
            
            if current_unit_price > 0:
                qty = int(target_budget // current_unit_price)
            else:
                qty = 0
                
            if qty > 0:
                print(f"총 매수 가능 금액({total_orderable_amount:,}원)의 10%인 {target_budget:,}원 규모로 {qty}주 매수를 진행합니다.")
                return self.order(code, qty, side="buy")
            else:
                print(f"매수 가능 수량이 부족합니다. (계산된 수량: {qty}주)")
                return None
                
        except Exception as e:
            print(f"매수 판단/실행 중 오류 발생: {e}")
            return None

    def order(self, code, qty, side="buy"):
        """주문 실행"""
        if not self.account:
            raise ValueError("계좌 객체가 활성화되지 않았습니다.")
            
        stock = self.api.stock(code)
        sleep(1) 
        try:
            if side.lower() == "buy":
                return stock.buy(qty=qty)
            else:
                return stock.sell(qty=qty) 
        except Exception as e:
            print(f"주문 실행 중 오류 발생: {e}")
            raise e