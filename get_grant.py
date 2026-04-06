# -*- coding: utf-8 -*-
import requests
import json
import os
from dotenv import load_dotenv  # 추가: .env 파일 로드용

# .env 파일을 읽어 환경 변수로 설정합니다.
load_dotenv()


def get_approval_key(app_key, app_secret):
    url = "https://openapivts.koreainvestment.com:29443/oauth2/Approval"
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "secretkey": app_secret
    }
    
    res = requests.post(url, headers=headers, data=json.dumps(body))
    res_data = res.json()
    
    # 에러 체크 로직 추가
    if "approval_key" in res_data:
        return res_data["approval_key"]
    else:
        print("!!! 승인키 발급 실패 !!!")
        print(f"응답 내용: {res_data}") # 여기서 에러 원인이 출력됩니다.
        return None
        
if __name__ == "__main__":
    appkey=os.getenv('KIS_VIRTUAL_APPKEY')
    secretkey=os.getenv('KIS_VIRTUAL_SECRETKEY')        
    # 사용 예시
    my_approval_key = get_approval_key(appkey,  secretkey)
    print(f"발급된 승인키: {my_approval_key}")    