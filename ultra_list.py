import asyncio
import json
import requests
import websockets
import os
from dotenv import load_dotenv

load_dotenv()

# --- [설정 세션: 실전투자 정보 입력] ---
APP_KEY = os.getenv("KIS_APPKEY") 
APP_SECRET = os.getenv("KIS_SECRETKEY")
USER_ID = os.getenv("KIS_ID") # 실전 HTS ID

# 실전투자용 엔드포인트
BASE_URL = "https://openapi.koreainvestment.com:9443"
WS_URL = "ws://ops.koreainvestment.com:21000"

# --- [1. 웹소켓용 실시간 접속키(Approval Key) 발급] ---
def get_approval_key():
    """실전투자 웹소켓 접속을 위한 승인키를 발급받습니다."""
    url = f"{BASE_URL}/oauth2/Approval"
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "secretkey": APP_SECRET
    }
    try:
        res = requests.post(url, headers=headers, data=json.dumps(body))
        res.raise_for_status()
        return res.json().get('approval_key')
    except Exception as e:
        print(f"Approval Key 발급 실패: {e}")
        return None

# --- [2. 실시간 데이터 파싱 함수 (psearch_result)] ---
def psearch_result(data_body):
    """
    실시간 조건검색 결과 파싱 (0번 조건식 전용)
    data_body 예시: '000660143005I000' (종목/시간/상태/SEQ 순서)
    """
    try:
        # 1. 기본 정보 슬라이싱
        code = data_body[:6]           # 종목코드 (6자)
        time = data_body[6:12]          # 발생시간 (6자)
        status = data_body[12:13]       # 편입/이탈 구분 (1자)
        
        # 2. 조건식 순번(SEQ) 추출 
        # KIS 실시간 포맷상 status(12번 인덱스) 바로 뒤에 SEQ가 붙습니다.
        # 보통 2자리에서 4자리 숫자로 들어오는데, '0'번인지 확인하기 위해 추출합니다.
        seq_raw = data_body[13:].strip() # 13번 인덱스부터 끝까지 (혹은 구분자 전까지)
        
        # '0' 또는 '00', '000' 등으로 들어올 수 있으므로 숫자로 변환하여 체크
        # 만약 문자열이 섞여 있다면 정규식이나 숫자 판별이 필요할 수 있습니다.
        try:
            current_seq = int(seq_raw[:2]) # 앞 2자리를 숫자로 변환 (00, 01 등)
        except ValueError:
            current_seq = -1 # 파싱 실패 시 제외

        # 3. 0번 조건식만 필터링 출력
        if current_seq == 0:
            status_name = "🟢 [편입]" if status == "I" else "🔴 [이탈]"
            print(f"✨ [SEQ:0 발견] {status_name} {code} | 시간: {time}")
            
            # --- 여기서 실전투자 매수/매도 함수를 호출하면 됩니다 ---
            # if status == "I": execute_buy_order(code)
            
    except Exception as e:
        print(f"데이터 파싱 중 오류 발생: {e} (Raw: {data_body})")

# [참고] 웹소켓 루프 내에서 호출 방식
# parts = response.split('|')
# if len(parts) >= 4:
#     psearch_result(parts[3])

# --- [3. 웹소켓 메인 루프] ---
async def start_websocket():
    approval_key = get_approval_key()
    if not approval_key: return

    async with websockets.connect(WS_URL) as websocket:
        print("✅ KIS 실전투자 서버 연결 성공")

        # [수정된 등록 요청] - 구조를 표준 가이드에 맞춤
        subscribe_data = {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",      # 이 부분이 누락되거나 잘못되면 에러 발생
                "tr_type": "1",       # 1: 등록
                "content-type": "utf-8"
            },
            "body": {
                "input": {
                    "tr_id": "H0STCNI0",
                    "tr_key": USER_ID
                }
            }
        }

        await websocket.send(json.dumps(subscribe_data))
        
        while True:
            try:
                response = await websocket.recv()
                
                if response.startswith('{'):
                    msg = json.loads(response)
                    # 설정 오류나 토큰 만료 메시지 확인용
                    print(f"📢 시스템 메시지: {msg.get('body', {}).get('msg1', msg)}")
                else:
                    # 실시간 데이터 파싱 (| 구분자로 쪼갠 후 4번째 필드 추출)
                    parts = response.split('|')
                    if len(parts) >= 4:
                        psearch_result(parts[3])

            except Exception as e:
                print(f"❌ 에러 발생: {e}")
                await asyncio.sleep(1) # 재연결 대기
if __name__ == "__main__":
    try:
        asyncio.run(start_websocket())
    except KeyboardInterrupt:
        print("\n감시를 종료합니다.")