# -*- coding: utf-8 -*-
import os
import requests
import time
from datetime import datetime
from dotenv import load_dotenv
from pykis import PyKis  # pykis 임포트

load_dotenv()

class KISTools:
    _instance = None
    last_api_call_time = 0  # TPS 방어용 전역 변수

    def __new__(cls):
        if not cls._instance:
            cls._instance = super(KISTools, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, 'initialized'): return
        self.initialized = True
        
        # 설정 로드
        self.appkey = os.getenv('KIS_APPKEY')
        self.secretkey = os.getenv('KIS_SECRETKEY')
        self.v_appkey = os.getenv('KIS_VIRTUAL_APPKEY')
        self.v_secretkey = os.getenv('KIS_VIRTUAL_SECRETKEY')
        self.v_account_raw = os.getenv('KIS_VIRTUAL_ACCOUNT')
        
        # ID 로드 (환경변수에 KIS_ID 또는 KIS_VIRTUAL_ID가 있어야 합니다)
        user_id = os.getenv('KIS_ID') or os.getenv('KIS_VIRTUAL_ID')
        
        # 서버 URL 설정
        self.real_url = "https://openapi.koreainvestment.com:9443"
        self.vts_url = "https://openapivts.koreainvestment.com:29443"
        
        # 1. 네이티브 API용 토큰 발급
        self.real_token = self._get_token(self.appkey, self.secretkey, self.real_url)
        
        # 2. PyKis 라이브러리 초기화 (id 파라미터 추가)
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
        self.account = self.api.account()
    def _get_token(self, key, secret, url):
        """접근 토큰 발급용 내부 함수"""
        res = requests.post(f"{url}/oauth2/tokenP", json={
            "grant_type": "client_credentials", "appkey": key, "appsecret": secret
        })
        return res.json().get('access_token')

    def _call_api_safe(self, func, *args, **kwargs):
        """핵심: 모든 API 호출 간격을 3.6초로 강제 유지"""
        current = time.time()
        elapsed = current - KISTools.last_api_call_time
        if elapsed < 3.6:
            time.sleep(3.6 - elapsed)
        
        KISTools.last_api_call_time = time.time()
        return func(*args, **kwargs)

    def get_market_data(self, code, timeframe="D"):
        """네이티브 API를 사용하여 시세 및 30일치 데이터 조회"""
        def _fetch():
            url = f"{self.real_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
            today = datetime.now().strftime("%Y%m%d")
            
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.real_token}",
                "appkey": self.appkey,
                "appsecret": self.secretkey,
                "tr_id": "FHKST03010100"
            }
            params = {
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": code,
                "fid_input_date_1": "20240101",
                "fid_input_date_2": today,
                "fid_period_div_code": timeframe,
                "fid_org_adj_prc": "1"
            }
            return requests.get(url, headers=headers, params=params).json()

        res_data = self._call_api_safe(_fetch)
        out1 = res_data.get('output1', {})
        out2 = res_data.get('output2', [])

        if not out2:
            error_msg = res_data.get('msg1', 'Unknown Error')
            raise ValueError(f"데이터를 가져오지 못했습니다: {error_msg}")

        recent = out2[:30]
        return {
            "price": float(out1.get('stck_prpr', 0)),
            "closes": [float(b['stck_clpr']) for b in recent[::-1]],
            "volumes": [float(b['acml_vol']) for b in recent[::-1]],
            "stock_name": out1.get('hts_kor_isnm', 'Unknown')
        }

    def buy_ten_percent(self, code):
        """PyKis 라이브러리를 사용하여 안전하게 10% 매수 실행"""
        def _logic():
            try:
                stock = self.api.stock(code)
                # 매수 가능 수량/금액 조회 (API 호출 발생)
                orderable = stock.orderable_amount()
                total_orderable_amount = float(orderable.amount)
                
                target_budget = total_orderable_amount * 0.1
                current_unit_price = float(orderable.unit_price)
                
                if current_unit_price > 0:
                    qty = int(target_budget // current_unit_price)
                else:
                    qty = 0
                    
                if qty > 0:
                    print(f"💰 [주문] 잔고 10%({target_budget:,.0f}원) -> {qty}주 매수 시작")
                    # 실제 주문 실행 (내부 order 호출)
                    return self.order(code, qty, side="buy")
                else:
                    print(f"⏳ [{code}] 매수 가능 수량이 부족합니다.")
                    return None
            except Exception as e:
                print(f"❌ 매수 판단 중 오류: {e}")
                return None

        # 로직 전체를 Safe Wrapper로 감싸 TPS 보호
        return self._call_api_safe(_logic)

    def order(self, code, qty, side="buy"):
        """PyKis를 통한 최종 주문 실행"""
        if not self.account:
            raise ValueError("계좌 객체가 활성화되지 않았습니다.")
            
        def _execute():
            stock = self.api.stock(code)
            try:
                if side.lower() == "buy":
                    # 시장가 주문 (01)
                    return stock.buy(qty=qty)
                else:
                    return stock.sell(qty=qty) 
            except Exception as e:
                print(f"❌ 주문 실행 중 오류: {e}")
                raise e

        # 실제 주문 통신도 Safe Wrapper 적용
        return self._call_api_safe(_execute)