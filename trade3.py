import json
import requests
import re
import os
import time
from time import sleep
from groq import Groq
from dotenv import load_dotenv

# 계산 및 알림 유틸리티 (라이브러리 간섭 없음)
from scripts.strategy import calculate_rsi
from scripts.notifier import DiscordNotifier

load_dotenv()

# ==============================
# 🔧 설정 및 서버 이원화
# ==============================
USER_ID = os.getenv("KIS_ID")

# 1. 실전 서버 (조건 검색 및 시세 데이터용)
REAL_APP_KEY = os.getenv("KIS_APPKEY")
REAL_APP_SECRET = os.getenv("KIS_SECRETKEY")
REAL_URL = "https://openapi.koreainvestment.com:9443"

# 2. 모의 서버 (매수 주문용)
VTS_APP_KEY = os.getenv("KIS_VIRTUAL_APPKEY")
VTS_APP_SECRET = os.getenv("KIS_VIRTUAL_SECRETKEY")
VTS_ACCOUNT = os.getenv("KIS_VIRTUAL_ACCOUNT") # 예: 50000000-01
VTS_URL = "https://openapivts.koreainvestment.com:29443"

POLLING_INTERVAL = 60 # 사이클 대기 시간

class Color:
    GREEN = "\033[92m"; YELLOW = "\033[93m"; RED = "\033[91m"
    CYAN = "\033[96m"; BOLD = "\033[1m"; BG_GREEN = "\033[42m\033[30m"
    RESET = "\033[0m"

# ==============================
# 🛡️ 전역 API 호출 제어 (TPS 차단기)
# ==============================
last_api_call_time = 0

def call_api_safe(api_func, *args, **kwargs):
    """모든 API 호출 사이에 최소 3.5초의 간격을 강제합니다."""
    global last_api_call_time
    MIN_INTERVAL = 3.6  # 모의투자 서버 안전권
    
    current_time = time.time()
    elapsed = current_time - last_api_call_time
    
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
        
    last_api_call_time = time.time()
    return api_func(*args, **kwargs)

# ==============================
# 📡 직접 구현 API 함수 (Native REST API)
# ==============================
def get_access_token(app_key, app_secret, url):
    endpoint = f"{url}/oauth2/tokenP"
    body = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    res = requests.post(endpoint, json=body)
    return res.json().get("access_token")

def fetch_psearch_codes_direct(token, user_id):
    """실전 서버에서 조건검색 결과를 가져옵니다 (기존 스캐너 대체)"""
    url = f"{REAL_URL}/uapi/domestic-stock/v1/trading/psearch-result"
    headers = {
        "Content-Type": "application/json", "Authorization": f"Bearer {token}",
        "appkey": REAL_APP_KEY, "appsecret": REAL_APP_SECRET,
        "tr_id": "HHKST03900400", "custtype": "P"
    }
    params = {"user_id": user_id, "seq": "0"} # seq 0번 검색식 기준
    res = requests.get(url, headers=headers, params=params)
    return [item['code'] for item in res.json().get('output2', [])]

def get_market_data_direct(token, stock_code):
    """실전 서버에서 RSI 및 분석용 차트 데이터를 가져옵니다."""
    url = f"{REAL_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    headers = {
        "Content-Type": "application/json", "Authorization": f"Bearer {token}",
        "appkey": REAL_APP_KEY, "appsecret": REAL_APP_SECRET, "tr_id": "FHKST03010100"
    }
    params = {
        "fid_cond_mrkt_div_code": "J", "fid_input_iscd": stock_code,
        "fid_period_div_code": "D", "fid_org_adj_prc": "1"
    }
    res = requests.get(url, headers=headers, params=params)
    out1 = res.json().get('output1', {})
    out2 = res.json().get('output2', [])
    
    return {
        "stock_name": out1.get('hts_kor_isnm', 'Unknown'),
        "price": float(out1.get('stck_prpr', 0)),
        "closes": [float(x['stck_clpr']) for x in out2[::-1]], # 과거->현재 순
        "volumes": [int(x['acml_vol']) for x in out2[::-1]]
    }

def execute_order_direct(vts_token, stock_code, qty=1):
    """모의투자 서버로 전량 현금 매수 주문을 보냅니다."""
    url = f"{VTS_URL}/uapi/domestic-stock/v1/trading/order-cash"
    headers = {
        "Content-Type": "application/json", "Authorization": f"Bearer {vts_token}",
        "appkey": VTS_APP_KEY, "appsecret": VTS_APP_SECRET,
        "tr_id": "VTTC0802U", "custtype": "P"
    }
    body = {
        "CANO": VTS_ACCOUNT[:8], "ACNT_PRDT_CD": VTS_ACCOUNT[-2:],
        "PDNO": stock_code, "ORD_DVSN": "01", "ORD_QTY": str(qty), "ORD_UNPR": "0"
    }
    return requests.post(url, json=body).json()

def safe_json_loads(text):
    try:
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return json.loads(match.group(0)) if match else {"action": "HOLD", "reason": "No JSON"}
    except:
        return {"action": "HOLD", "reason": "Parse Error"}

# ==============================
# 📈 분석 루프 (사용자 요청 로직 적용)
# ==============================
def process_stock_analysis(stock_code, real_token, vts_token):
    try:
        notifier = DiscordNotifier()
        client = Groq(api_key=os.getenv("GROQ_API_KEY"))

        # 1. 시세 데이터 수집 (실전 데이터 사용)
        data = call_api_safe(get_market_data_direct, real_token, stock_code)
        stock_name = data['stock_name']        
        print(f"{Color.CYAN}🔍 [Analysis] [{stock_name}({stock_code})] 분석 시작...{Color.RESET}")
        
        rsi = calculate_rsi(data['closes'])
        recent_volumes = data['volumes'][-5:]
        avg_volume = sum(data['volumes']) / len(data['volumes'])

        # 2. AI 판단 요청
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
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_input}],
            temperature=0
        )

        decision = safe_json_loads(response.choices[0].message.content)
        action = decision.get("action", "HOLD").upper()
        reason = decision.get("reason", "No reason provided")

        color = Color.BG_GREEN if action == "BUY" else Color.YELLOW
        print(f"{color}💡 [{stock_code}] AI 판단: {action}{Color.RESET} | 사유: {reason}")

        # 3. 매수 실행 (모의투자 서버에 주문)
        if action == "BUY":
            print(f"{Color.GREEN}✅ [{stock_code}] 모의투자 매수 주문 전송...{Color.RESET}")
            res = call_api_safe(execute_order_direct, vts_token, stock_code, qty=1)
            if res.get('rt_cd') == '0':
                notifier.send("AUTO BUY", f"✅ **매수 완료: {stock_name}**\n- 사유: {reason}")
            else:
                print(f"❌ 주문 실패: {res.get('msg1')}")
        else:
            print(f"{Color.YELLOW}⏳ [{stock_code}] 관망 결정{Color.RESET}")

    except Exception as e:
        print(f"{Color.RED}⚠️ [{stock_code}] 분석 중 오류: {e}{Color.RESET}")

# ==============================
# 🏁 메인 실행부
# ==============================
if __name__ == "__main__":
    print(f"{Color.BOLD}🚀 실전 검색 & 모의 주문 듀얼 시스템 가동{Color.RESET}")
    
    # 서버별 독립 토큰 발급
    real_token = get_access_token(REAL_APP_KEY, REAL_APP_SECRET, REAL_URL)
    vts_token = get_access_token(VTS_APP_KEY, VTS_APP_SECRET, VTS_URL)

    try:
        while True:
            print(f"\n{Color.YELLOW}📡 조건 검색 스캔 중...{Color.RESET}")
            # 실전 서버에서 타겟 종목 추출
            target_codes = call_api_safe(fetch_psearch_codes_direct, real_token, USER_ID)
            
            if target_codes:
                print(f"✨ {len(target_codes)}개 종목 포착. 순차 분석을 시작합니다.")
                for code in target_codes:
                    process_stock_analysis(code, real_token, vts_token)
            else:
                print("조건에 맞는 종목이 없습니다.")

            print(f"\n{Color.CYAN}💤 한 사이클 완료. {POLLING_INTERVAL}초 대기...{Color.RESET}")
            sleep(POLLING_INTERVAL)
            
    except KeyboardInterrupt:
        print("\n[운영 중단] 시스템을 안전하게 종료합니다.")