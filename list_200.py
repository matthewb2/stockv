# -*- coding: utf-8 -*-
import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()

# ⚠️ 실전 투자용 키로 변경하세요
APP_KEY = os.getenv("KIS_APPKEY") 
APP_SECRET = os.getenv("KIS_SECRETKEY")
USER_ID = os.getenv("KIS_ID") # 실전 HTS ID

# ⚠️ 실전 도메인 (9443)
URL_BASE = "https://openapi.koreainvestment.com:9443"

def get_real_token():
    url = f"{URL_BASE}/oauth2/tokenP"
    body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
    res = requests.post(url, json=body)
    return res.json().get("access_token")

def check_real_psearch():
    token = get_real_token()
    path = "/uapi/domestic-stock/v1/quotations/psearch-title"
    
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "HHKST03900300",
        "custtype": "P"
    }
    params = {"user_id": USER_ID}

    print(f"[*] [실전서버] {USER_ID}님의 조건목록 확인 중...")
    res = requests.get(f"{URL_BASE}{path}", headers=headers, params=params)
    data = res.json()
    
    if 'output2' in data and data['output2']:
        print("✅ 실전 서버에서 데이터를 찾았습니다!")
        for item in data['output2']:
            print(f" SEQ: {item['seq']} | 명칭: {item['condition_nm']}")
    else:
        print(f"❌ 실전에서도 실패: {data.get('msg1')}")

if __name__ == "__main__":
    check_real_psearch()