import json
import requests
import os
import time
from time import sleep
from dotenv import load_dotenv
from scripts.kis_tools import KISTools
from scripts.notifier import DiscordNotifier

load_dotenv()

# ==============================
# 🔧 설정 및 유틸리티
# ==============================
APP_KEY = os.getenv("KIS_VIRTUAL_APPKEY")
APP_SECRET = os.getenv("KIS_VIRTUAL_SECRETKEY")
ACCOUNT_FRONT = os.getenv("KIS_VIRTUAL_ACCOUNT")[:8]
ACCOUNT_BACK = os.getenv("KIS_VIRTUAL_ACCOUNT")[-2:]
BASE_URL = "https://openapivts.koreainvestment.com:29443"

TARGET_PROFIT = 10.0
STOP_LOSS = -6.0
CHECK_INTERVAL = 60 

class Color:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    RESET = "\033[0m"

# ==============================
# 🛡️ 정밀 API 호출 제어 (TPS 차단기)
# ==============================
last_api_call_time = 0

def call_api_safe(api_func, *args, **kwargs):
    global last_api_call_time
    # 모의투자 서버 부하 분산을 위해 간격을 4초로 상향 조정
    MIN_INTERVAL = 4.1 
    
    current_time = time.time()
    elapsed = current_time - last_api_call_time
    
    if elapsed < MIN_INTERVAL:
        sleep(MIN_INTERVAL - elapsed)
        
    last_api_call_time = time.time()
    return api_func(*args, **kwargs)

def get_kis_access_token():
    url = f"{BASE_URL}/oauth2/tokenP"
    payload = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    res = requests.post(url, data=json.dumps(payload))
    return res.json().get('access_token')
    
# ==============================
# 📡 직접 구현 API 함수 (라이브러리 미사용)
# ==============================
def get_access_token():
    url = f"{BASE_URL}/oauth2/tokenP"
    payload = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
    res = requests.post(url, data=json.dumps(payload))
    return res.json().get('access_token')

def get_current_price(token, stock_code):
    """라이브러리 대신 직접 현재가를 조회하여 내부 호출을 차단합니다."""
    path = "/uapi/domestic-stock/v1/quotations/inquire-price"
    url = f"{BASE_URL}{path}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010100" # 주식 현재가 시세 tr_id
    }
    params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": stock_code}
    res = requests.get(url, headers=headers, params=params)
    return res.json().get('output', {})

def get_balance(token):
    url = f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "VTTC8434R", # 모의투자 잔고 tr_id
        "custtype": "P"
    }
    params = {
        "CANO": ACCOUNT_FRONT, "ACNT_PRDT_CD": ACCOUNT_BACK,
        "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
        "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""
    }
    res = requests.get(url, headers=headers, params=params)
    data = res.json()
    return data.get('output1', []) if data.get('rt_cd') == '0' else []

def sell_stock_native(token, stock_code, qty):
    """라이브러리 없이 네이티브 API로 시장가 매도 주문을 실행합니다."""
    path = "/uapi/domestic-stock/v1/trading/order-cash"
    url = f"{BASE_URL}{path}"
    
    # 모의투자 매도 tr_id: VTTC0801U (매수와 다름)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "VTTC0801U",  # 국내주식 모의투자 매도 주문
        "custtype": "P",
        "hashkey": "" # 해쉬키는 생략 가능하나 보안상 필요시 추가 가능
    }
    
    payload = {
        "CANO": ACCOUNT_FRONT,
        "ACNT_PRDT_CD": ACCOUNT_BACK,
        "PDNO": stock_code,
        "ORD_DVSN": "01",      # 01: 시장가
        "ORD_QTY": str(qty),   # 주문 수량 (문자열)
        "ORD_UNPR": "0",       # 시장가는 가격 0
    }
    
    res = requests.post(url, headers=headers, data=json.dumps(payload))
    return res.json()
    
# ==============================
# 🏁 메인 실행 로직
# ==============================
if __name__ == "__main__":
    print(f"{Color.CYAN}🚀 라이브러리 간섭 차단형 무소음 자동매매 시스템 가동{Color.RESET}")
    
    try:
        # 최초 토큰 발급
        access_token = get_kis_access_token()
        kis = KISTools()
        notifier = DiscordNotifier()
        if not access_token:
            print("❌ 토큰 발급 실패."); exit()

        while True:
            print(f"\n{Color.YELLOW}🔍 계좌 잔고 스캔 시작...{Color.RESET}")
            
            # 1. 잔고 조회 (안전 호출)
            balance_list = call_api_safe(get_balance, access_token)
            
            if not balance_list:
                print("보유 종목 없음")
            else:
                for item in balance_list:
                    try:
                        stock_code = item['pdno']
                        stock_name = item['prdt_name']
                        qty = int(item['hldg_qty'])
                        if qty <= 0: continue

                        # 2. 현재가 직접 조회 (라이브러리 kis_tools 대신 직접 API 호출)
                        # 이 부분이 경고의 핵심 원인이었으므로 직접 통제합니다.
                        price_data = call_api_safe(get_current_price, access_token, stock_code)
                        curr_price = float(price_data.get('stck_prpr', 0))
                        
                        if curr_price == 0: continue

                        buy_price = float(item['pchs_avg_pric'])
                        profit_rate = ((curr_price - buy_price) / buy_price) * 100 - 0.3
                        
                        status_color = Color.GREEN if profit_rate > 0 else Color.RED
                        print(f"📊 [{stock_name}({stock_code})] 매수가: {buy_price:,.0f} | 현재가: {curr_price:,.0f} | 수익률: {status_color}{profit_rate:.2f}%{Color.RESET}")
                        # 매도 판단
                        if profit_rate >= TARGET_PROFIT or profit_rate <= STOP_LOSS:
                            reason = "익절" if profit_rate >= TARGET_PROFIT else "손절"
                            sell_color = Color.YELLOW if reason == "익절" else Color.RED
                                    
                            print(f"{sell_color}🚀 {reason} 조건 도달! 네이티브 전량 매도 실행{Color.RESET}")
                            
                            # 📍 네이티브 함수 호출 (kis.order 대체)
                            order_res = call_api_safe(sell_stock_native, access_token, stock_code, qty)
                            
                            if order_res.get('rt_cd') == '0':
                                print(f"{Color.GREEN}✅ 매도 주문 성공: {order_res.get('msg1')}{Color.RESET}")
                                notifier.send("AUTO SELL", f"🧪 **네이티브 자동 매도 ({reason})**\n종목: {stock_name}\n수익률: {profit_rate:.2f}%")
                            else:
                                print(f"{Color.RED}❌ 매도 주문 실패: {order_res.get('msg1')}{Color.RESET}")                 
                            
                            
                        # 📍 종목 간 TPS 방어용 지연 (매 루프마다 실행)
                        sleep(1)

                    except Exception as e:
                        print(f"⚠️ 에러: {e}")
                        continue

            print(f"\n{Color.CYAN}💤 사이클 종료. {CHECK_INTERVAL}초 대기...{Color.RESET}")
            sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\n종료합니다.")