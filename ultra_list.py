# -*- coding: utf-8 -*-
import requests
import json
import os
import asyncio
import websockets
from dotenv import load_dotenv

load_dotenv()

APP_KEY = os.getenv("KIS_APPKEY")
APP_SECRET = os.getenv("KIS_SECRETKEY")
USER_ID = os.getenv("KIS_ID")

URL_BASE = "https://openapi.koreainvestment.com:9443"
WS_BASE = "ws://ops.koreainvestment.com:21000" # 실전 투자용

def get_approval_key():
    url = f"{URL_BASE}/oauth2/Approval"
    headers = {"content-type": "application/json; charset=utf-8"}
    body = {"grant_type": "client_credentials", "appkey": APP_KEY, "secretkey": APP_SECRET}
    res = requests.post(url, headers=headers, json=body)
    return res.json().get("approval_key")

async def connect_websocket():
    approval_key = get_approval_key()
    if not approval_key:
        print("❌ Approval Key 발급 실패")
        return

    async with websockets.connect(WS_BASE) as websocket:
        # [공식 문서 규격] 실시간 조건검색 등록 데이터
        subscribe_data = {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",      # 개인
                "tr_type": "1",       # 1: 등록, 2: 해제
                "content-type": "utf-8"
            },
            "body": {
                "input": {
                    "tr_id": "HHKST03900300", # 실시간 조건검색 전용 ID
                    "tr_key": USER_ID         # 공식 문서상 tr_key는 HTS ID만 입력
                }
            }
        }
        
        await websocket.send(json.dumps(subscribe_data))
        print(f"[*] 실시간 조건검색 감시 시작 (ID: {USER_ID})...")

        while True:
            try:
                data = await websocket.recv()
                
                # 1. PINGPONG 처리 (접속 유지 필수)
                if "PINGPONG" in data:
                    await websocket.send(data)
                    continue

                # 2. 시스템 응답 (JSON 형태)
                if data.startswith('{'):
                    res = json.loads(data)
                    msg = res.get("body", {}).get("msg1")
                    if msg:
                        print(f"📢 [시스템]: {msg}")
                    continue

                # 3. 실시간 종목 데이터 (가변길이 문자열)
                # 데이터 예시: 0|HHKST03900300|001|mkbang79^152501^I^005930^삼성전자^...
                parts = data.split('|')
                
                # 데이터 구분자 '0' 혹은 '1' 확인
                if len(parts) >= 4 and parts[1] == "HHKST03900300":
                    # 실시간 데이터 본문 (tr_key 이후 섹션)
                    core_data = parts[3]
                    elements = core_data.split('^')
                    
                    # 공식 문서 데이터 레이아웃 (HHKST03900300)
                    # elements[0] : 고객ID
                    # elements[1] : 발생시간 (HHMMSS)
                    # elements[2] : 등록/해제 구분 (I:편입, D:이탈)
                    # elements[3] : 종목코드 (앞의 'A' 제거 필요할 수 있음)
                    # elements[4] : 종목명
                    
                    time_val = elements[1]
                    status = "✅ 편입" if elements[2] == "I" else "❌ 이탈"
                    code = elements[3].strip()
                    name = elements[4].strip() if len(elements) > 4 else "Unknown"
                    
                    print(f"🚀 [{time_val}] {status} | {code} | {name}")

            except Exception as e:
                print(f"⚠️ 에러 발생: {e}")
                print(f"DEBUG 원본 데이터: {data}")
                break

if __name__ == "__main__":
    asyncio.run(connect_websocket())