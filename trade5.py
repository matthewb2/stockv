import json
import requests
import re
import os
from time import sleep
import time
from groq import Groq
from dotenv import load_dotenv

# 기존 프로젝트 모듈
from scripts.kis_tools import KISTools
from scripts.scanner import KISScanner, Color  # 분리된 모듈 임포트
from scripts.strategy import calculate_rsi
from scripts.notifier import DiscordNotifier

load_dotenv()

# ==============================
# 🔧 설정 및 유틸리티
# ==============================
USER_ID = os.getenv("KIS_ID")
APP_KEY = os.getenv("KIS_APPKEY")
APP_SECRET = os.getenv("KIS_SECRETKEY")
BASE_URL = "https://openapi.koreainvestment.com:9443"

# ==============================
# 🔧 설정 (실전/모의 이원화)
# ==============================
USER_ID = os.getenv("KIS_ID")
# 실전 계좌 정보 (검색용)
REAL_APP_KEY = os.getenv("KIS_APPKEY")
REAL_APP_SECRET = os.getenv("KIS_SECRETKEY")
REAL_URL = "https://openapi.koreainvestment.com:9443"

# 모의투자 계좌 정보 (주문용)
VTS_APP_KEY = os.getenv("KIS_VIRTUAL_APPKEY")
VTS_APP_SECRET = os.getenv("KIS_VIRTUAL_SECRETKEY")
VTS_ACCOUNT = os.getenv("KIS_VIRTUAL_ACCOUNT") # 예: 50000000-01
VTS_URL = "https://openapivts.koreainvestment.com:29443"

# 매매 파라미터
TARGET_PROFIT = 10.0
STOP_LOSS = -6.0
API_DELAY = 60       # API 유량 제한 방지 지연 (초)
POLLING_INTERVAL = 60 # 전체 루프 주기

def extract_json(text):
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match: raise ValueError("JSON 추출 실패")
    return match.group(0)

def safe_json_loads(text):
    try: 
        return json.loads(extract_json(text))
    except: 
        return {"action": "HOLD", "reason": "JSON 파싱 에러"}

# ==============================
# 🎨 콘솔 색상 정의
# ==============================
class Color:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    BOLD = "\033[1m"
    BG_GREEN = "\033[42m\033[30m"
    RESET = "\033[0m"

def call_api_safe(api_func, *args, **kwargs):
    """모든 서버 호출 간격을 최소 3.5초로 강제 고정합니다."""
    global last_api_call_time
    MIN_INTERVAL = 3.5 
    
    current_time = time.time()
    elapsed = current_time - last_api_call_time
    
    if elapsed < MIN_INTERVAL:
        sleep(MIN_INTERVAL - elapsed)
        
    last_api_call_time = time.time()
    return api_func(*args, **kwargs)
    
# ==============================
# 📈 분석 및 매수 함수 (순차 실행용)
# ==============================

def get_market_data_direct(token, stock_code):
    """실전 서버에서 차트/현재가 데이터를 직접 가져옵니다."""
    url = f"{REAL_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}", "appkey": REAL_APP_KEY, "appsecret": REAL_APP_SECRET, "tr_id": "FHKST03010100"}
    params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": stock_code, "fid_period_div_code": "D", "fid_org_adj_prc": "1"}
    res = requests.get(url, headers=headers, params=params)
    out2 = res.json().get('output2', [])
    return {
        "stock_name": res.json().get('output1', {}).get('hts_kor_isnm', 'Unknown'),
        "price": float(out2[0]['stck_clpr']) if out2 else 0,
        "closes": [float(x['stck_clpr']) for x in out2[::-1]],
        "volumes": [int(x['acml_vol']) for x in out2[::-1]]
    }
    
def process_stock_analysis(stock_code, token):
    """포착된 종목을 직접 API 호출로 분석하고 매수 판단"""
    try:
        # 객체 생성은 루프 밖에서 하는 것이 효율적이나, 구조 유지를 위해 배치
        notifier = DiscordNotifier()
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))

        # 1. 시세 데이터 수집 (안전 래퍼 적용)
        # kis.get_market_data 대신 직접 구현한 함수(get_market_data_direct)를 사용합니다.
        data = call_api_safe(get_market_data_direct, token, stock_code)
        
        stock_name = data['stock_name']        
        print(f"{Color.CYAN}🔍 [Analysis] [{stock_name}({stock_code})] 분석 시작...{Color.RESET}")
        
        # 지표 계산
        rsi = calculate_rsi(data['closes'])
        recent_volumes = [int(v) for v in data['volumes'][-5:]]
        avg_volume = int(sum(data['volumes']) / len(data['volumes']))

        # 2. AI 판단 요청 (Groq API는 KIS TPS와 무관하므로 바로 호출 가능)
        system_prompt = (
            "You are an expert stock trader. Analyze technical indicators and respond ONLY in JSON. "
            "Structure: {\"action\": \"BUY\" or \"HOLD\", \"reason\": \"...\"}"
        )
        user_input = (
            f"Stock: {stock_code}, Price: {data['price']:,.0f}\n"
            f"RSI: {rsi:.2f}, Volumes: {recent_volumes}, Avg: {avg_volume}"
        )

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input}
            ],
            temperature=0
        )

        decision = safe_json_loads(response.choices[0].message.content)
        action = decision.get("action", "HOLD").upper()
        reason = decision.get("reason", "No reason provided")

        color = Color.BG_GREEN if action == "BUY" else Color.YELLOW
        print(f"{color}💡 [{stock_code}] AI 판단: {action}{Color.RESET} | 사유: {reason}")

        # 3. 매수 실행 (모의투자용 토큰과 URL을 사용하여 안전하게 호출)
        if action == "BUY":
            print(f"{Color.GREEN}✅ [{stock_code}] 매수 주문 실행 중...{Color.RESET}")
            # kis.buy_ten_percent 대신 직접 구현한 주문 함수 사용
            call_api_safe(execute_order_direct, vts_token, stock_code, qty=1) 
            notifier.send("AUTO BUY", f"✅ **매수 완료: {stock_name}**\n- 사유: {reason}")
        else:
            print(f"{Color.YELLOW}⏳ [{stock_code}] 관망 결정{Color.RESET}")

    except Exception as e:
        print(f"{Color.RED}⚠️ [{stock_code}] 분석 중 오류: {e}{Color.RESET}")
        
# ==============================
# 🔍 조건검색 REST API
# ==============================
_SEARCH_TOKEN = None

def get_search_token():
    global _SEARCH_TOKEN
    if _SEARCH_TOKEN: return _SEARCH_TOKEN

    url = f"{BASE_URL}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    try:
        res = requests.post(url, json=body)
        _SEARCH_TOKEN = res.json().get("access_token")
        return _SEARCH_TOKEN
    except Exception as e:
        print(f"❌ 검색 토큰 발급 실패: {e}")
        return None

# ==============================
# 🏁 메인 루프
# ==============================
if __name__ == "__main__":
    print(f"{Color.BOLD}🚀 순차 실행 방식 자동매매 시스템 가동{Color.RESET}")
    
    main_kis = KISTools()
    sleep(5)
    # 스캐너 객체 생성
    scanner = KISScanner(BASE_URL, APP_KEY, APP_SECRET, USER_ID)
    
    try:
        while True:
            # 1. 조건검색 종목 코드 리스트 가져오기
            target_codes = scanner.fetch_psearch_stocks()
            
            #fetch_psearch_and_run(main_kis)
            if target_codes:
                print(f"✨ {len(target_codes)}개 종목 포착. 분석을 시작합니다.")
                for code in target_codes:
                    # 2. 각 종목 순차 분석
                    sleep(1.0)
                    process_stock_analysis(code, main_kis.token)
                    sleep(1.0) # 종목 간 안전 지연
            
            print(f"\n{Color.CYAN}⏳ 한 사이클 완료. {POLLING_INTERVAL}초 대기...{Color.RESET}")
            sleep(POLLING_INTERVAL)
            
    except KeyboardInterrupt:
        print("\n[운영 중단] 시스템을 종료합니다.")