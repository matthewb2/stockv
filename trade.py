# -*- coding: utf-8 -*-
import json
import requests
import re
import os
import time
from datetime import datetime
from groq import Groq
from dotenv import load_dotenv

from scripts.kis_tools import KISTools
from scripts.scanner import KISScanner
from scripts.strategy import calculate_rsi
from scripts.notifier import DiscordNotifier

load_dotenv()

APP_KEY = os.getenv("KIS_VIRTUAL_APPKEY")
APP_SECRET = os.getenv("KIS_VIRTUAL_SECRETKEY")
ACCOUNT_FRONT = os.getenv("KIS_VIRTUAL_ACCOUNT")[:8]
ACCOUNT_BACK = os.getenv("KIS_VIRTUAL_ACCOUNT")[-2:]
BASE_URL = "https://openapivts.koreainvestment.com:29443"

# ==============================
# 🎨 콘솔 색상 및 유틸리티
# ==============================
class Color:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    BG_GREEN = "\033[42m\033[30m"
    RESET = "\033[0m"

def safe_json_loads(text):
    try:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return json.loads(match.group(0)) if match else {"action": "HOLD"}
    except:
        return {"action": "HOLD", "reason": "JSON Parsing Error"}

# ==============================
# 🔑 실전 토큰 발급 (조회 전용)
# ==============================
def get_real_market_token():
    """시세 및 검색 데이터 조회를 위한 실전 토큰"""
    url = "https://openapi.koreainvestment.com:9443/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": os.getenv("KIS_APPKEY"),
        "appsecret": os.getenv("KIS_SECRETKEY")
    }
    res = requests.post(url, json=body)
    return res.json().get("access_token")

# ==============================
# 📈 분석 및 매수 실행부
# ==============================
def process_stock_analysis(stock_code, kis):
    try:
        notifier = DiscordNotifier()
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))

        # 1. 중복 매수 확인 (모의투자 계좌 잔고 조회)
        balance_obj = get_balance(get_access_token())
        holding_codes = []
        if hasattr(balance_obj, 'holdings'):
            holding_codes = [h.code for h in balance_obj.holdings]
        
        if stock_code in holding_codes:
            print(f"{Color.YELLOW}⚠️ [{stock_code}] 이미 모의계좌에 보유 중인 종목입니다.{Color.RESET}")
            return

        print(f"모의투자 계좌 보유 종목 확인 완료")
        # 2. 데이터 수집 (실전 토큰 기반 시세 조회)
        data = kis.get_market_data(stock_code)
        stock_name = data.get('stock_name', 'Unknown')
        rsi = calculate_rsi(data['closes'])
        
        print(f"{Color.CYAN}🔍 [Analysis] {stock_name}({stock_code}) | RSI: {rsi:.2f}{Color.RESET}")

        # 3. AI 의사결정
        system_prompt = "You are a stock trader. Respond ONLY in JSON: {\"action\": \"BUY\" or \"HOLD\", \"reason\": \"...\"}"
        user_input = f"Stock: {stock_name}, Price: {data['price']}, RSI: {rsi:.2f}. BUY or HOLD?"

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_input}],
            temperature=0
        )

        decision = safe_json_loads(response.choices[0].message.content)
        action = decision.get("action", "HOLD").upper()

        # 4. 매수 주문 (모의투자 계좌 실행)
        if action == "BUY":
            print(f"{Color.BG_GREEN}✅ AI BUY SIGNAL: {stock_name}{Color.RESET}")
            order_res = kis.buy_ten_percent(stock_code) # PyKis를 통한 모의투자 주문
            if order_res:
                notifier.send("AUTO BUY (VIRTUAL)", f"✅ **모의투자 매수 완료: {stock_name}**\n- 사유: {decision.get('reason')}")
        else:
            print(f"{Color.YELLOW}⏳ 관망 (HOLD){Color.RESET}")

    except Exception as e:
        print(f"⚠️ [{stock_code}] 오류: {e}")

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


def get_holding_codes_native():
    """
    네이티브 API를 사용하여 모의투자 계좌의 보유 종목 코드 리스트를 반환합니다.
    """
    # 1. 헤더 설정 (모의투자는 tr_id가 VTRP3112R)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}", # PyKis가 관리하는 토큰 재활용
        "appkey": os.getenv('KIS_VIRTUAL_APPKEY'),
        "appsecret": os.getenv('KIS_VIRTUAL_SECRETKEY'),
        "tr_id": "VTRP3112R", # 모의투자 주식잔고조회 TR ID
        "custtype": "P"
    }

    # 2. 파라미터 설정
    # 계좌번호 앞 8자리와 뒤 2자리를 분리 (예: 12345678-01)
    acc_no = os.getenv('KIS_VIRTUAL_ACCOUNT').split('-')
    
    params = {
        "CANO": acc_no[0],           # 계좌번호 8자리
        "ACNT_PRDT_CD": acc_no[1],   # 계좌상품코드 2자리
        "AFHR_FLG": "N",             # 시간외단일가여부
        "OFL_YN": "",                # 오프라인여부
        "INQR_DVSN": "02",           # 조회구분 (02: 종목별)
        "UNPR_DVSN": "01",           # 단가구분
        "FUND_STTL_ICRT_YN": "N",    # 펀드결제이익률여부
        "FNCG_AMT_AUTO_RDPT_YN": "N",# 융자금액자동상환여부
        "PRCS_DVSN": "00",           # 처리구분 (00: 전일매수포함)
        "CTX_AREA_FK100": "",        # 연속조회키
        "CTX_AREA_NK100": ""         # 연속조회키
    }

    def _fetch():
        # 모의투자용 URL(vts_url) 사용 필수
        url = f"{vts_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        res = requests.get(url, headers=headers, params=params)
        return res.json()

    try:
        res_data = self._call_api_safe(_fetch)
        
        # 3. 데이터 파싱
        output1 = res_data.get('output1', [])
        holding_codes = [item['pdno'] for item in output1 if int(item['hldg_qty']) > 0]
        
        return holding_codes

    except Exception as e:
        print(f"{Color.RED}❌ 네이티브 잔고 조회 중 오류: {e}{Color.RESET}")
        return []
        
# ==============================
# 🏁 메인 실행부
# ==============================
if __name__ == "__main__":
    print(f"{Color.BOLD}🚀 모의투자 기반 AI 자동매매 시스템 시작{Color.RESET}")
    
    # 1. KISTools 인스턴스 생성 (모의투자 계좌 정보 자동 로드)
    # PyKis(id=KIS_VIRTUAL_ID...) 로직이 내부에 포함되어 있음
    kis = KISTools()
    #print(get_balance(get_access_token()))
    
    # 2. 조회용 실전 데이터 속성 수동 주입
    # get_market_data 함수가 내부적으로 사용하는 실전 키 세팅
    kis.real_token = get_real_market_token()
    kis.appkey = os.getenv("KIS_APPKEY")
    kis.secretkey = os.getenv("KIS_SECRETKEY")

    # 3. 스캐너 초기화 (실전 계좌로 종목 발굴)
    scanner = KISScanner(
        "https://openapi.koreainvestment.com:9443", 
        os.getenv("KIS_APPKEY"), 
        os.getenv("KIS_SECRETKEY"), 
        os.getenv("KIS_ID")
    )
    
    try:
        while True:
            target_codes = scanner.fetch_psearch_stocks()
            
            if target_codes:
                print(f"✨ {len(target_codes)}개 종목 분석 시작 (TPS 3.6s 보호 적용)")
                for code in target_codes:
                    process_stock_analysis(code, kis)
                    print(f"☕ 다음 분석 전 대기 중...")
                    time.sleep(5)
            
            print(f"\n{Color.CYAN}⏳ 사이클 종료. 60초 대기...{Color.RESET}")
            time.sleep(60)
            
    except KeyboardInterrupt:
        print("\n👋 시스템을 종료합니다.")