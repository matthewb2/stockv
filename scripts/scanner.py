# -*- coding: utf-8 -*-
import requests


def _to_float(value):
    """KIS 응답 수치는 ' 3970121479.1210' 처럼 문자열 + 공백이 포함된다."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _normalize_code(value):
    """종목코드를 6자리 숫자로 정규화

    KIS는 코스닥 코드를 'A247540' 처럼 시장 표시 문자를 붙여 내려주기도 하며,
    앞뒤 공백이 섞여 온다. 숫자 6자리만 남기고, 그 외 형태는 None 을 반환한다.
    """
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits if len(digits) == 6 else None


# 콘솔 색상 (메인과 동일하게 사용하거나 임포트)
class Color:
    MAGENTA = "\033[95m"
    RED = "\033[91m"
    RESET = "\033[0m"


class KISScanner:
    """KIS 조건검색(psearch-result) API 래퍼

    HTS에 저장된 조건검색식(seq)으로 대상 종목 목록을 조회한다.

    token_provider를 넘기면 이미 발급받은 액세스 토큰을 재사용한다.
    (조건검색 전용 tokenP 발급은 1분당 1회 제한이 있으므로 권장)
    """

    BASE_URL_REAL = "https://openapi.koreainvestment.com:9443"
    BASE_URL_VTS = "https://openapivts.koreainvestment.com:29443"

    PATH_PSEARCH = "/uapi/domestic-stock/v1/quotations/psearch-result"
    TR_ID_PSEARCH = "HHKST03900400"
    MAX_PAGES = 20  # 연속조회 안전 장치

    def __init__(self, base_url, app_key, app_secret, user_id, virtual=False, token_provider=None):
        """
        Args:
            user_id: HTS 아이디 (조건검색 필수, 대문자)
            virtual: True면 한투 모의계좌 서버/토큰 사용 (기본값은 실전)
            token_provider: () -> token 형태의 콜백 (없으면 내부에서 tokenP 발급)
        """
        self.virtual = virtual
        self.base_url = base_url or (self.BASE_URL_VTS if virtual else self.BASE_URL_REAL)
        self.token_path = "/oauth2/tokenP" if virtual else "/oauth2/token"
        self.app_key = app_key
        self.app_secret = app_secret
        # 대문자 변환 금지: HTS 아이디는 소문자 그대로 사용해야 한다
        # (대문자로 바꾸면 rt_cd=1 MCA05762 로 실패한다)
        self.user_id = (user_id or "").strip()
        self.token_provider = token_provider
        self.token = None

    def _get_search_token(self):
        """조건검색에 사용할 토큰 (외부 토큰 우선, 없으면 tokenP 발급)"""
        if self.token_provider:
            return self.token_provider()

        if self.token:
            return self.token

        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }

        try:
            res = requests.post(f"{self.base_url}{self.token_path}", data=body, timeout=10)
            res.raise_for_status()

            data = res.json()
            self.token = data.get("access_token")

            if not self.token:
                print(f"{Color.RED}❌ 조건검색 토큰 발급 실패: {data}{Color.RESET}")
                return None

            return self.token
        except Exception as e:
            print(f"{Color.RED}❌ 조건검색 토큰 발급 실패: {e}{Color.RESET}")
            return None

    def _fetch_psearch_page(self, seq, tr_cont=""):
        """조건검색 1페이지 조회 → (목록, 다음 tr_cont, 응답 메타) 반환"""
        token = self._get_search_token()
        if not token:
            return [], "", {}

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": self.TR_ID_PSEARCH,
            "custtype": "P",
            "tr_cont": tr_cont,  # 연속조회 시 "N" 사용
        }

        params = {"user_id": self.user_id, "seq": str(seq)}

        try:
            res = requests.get(f"{self.base_url}{self.PATH_PSEARCH}",
                               headers=headers, params=params, timeout=10)
        except Exception as e:
            print(f"{Color.RED}❌ 조건검색 요청 실패: {e}{Color.RESET}")
            return [], "", {}

        if res.status_code != 200:
            print(f"{Color.RED}❌ 조건검색 실패: {res.status_code} | {res.text[:200]}{Color.RESET}")
            return [], "", {}

        data = res.json()
        meta = {
            "rt_cd": data.get("rt_cd"),
            "msg_cd": data.get("msg_cd", ""),
            "msg1": data.get("msg1", ""),
        }

        return data.get('output2') or [], res.headers.get('tr_cont', ''), meta

    def fetch_psearch_detail(self, seq=0):
        """조건검색 결과(연속조회 포함)를 [{code, name, price}, ...] 형태로 반환"""
        results = []
        tr_cont = ""
        meta = {}

        for page in range(self.MAX_PAGES):
            stocks, tr_cont, meta = self._fetch_psearch_page(seq, tr_cont)

            for item in stocks:
                code = _normalize_code(item.get('code') or item.get('s_code'))
                if not code:
                    continue
                results.append({
                    "code": code,
                    "name": item.get('name', ''),
                    "price": _to_float(item.get('price')),
                    "trade_amt": _to_float(item.get('trade_amt')),
                    "chgrate": _to_float(item.get('chgrate')),
                })

            # rt_cd '0' 이면 마지막 페이지
            if meta.get("rt_cd") == '0' or not tr_cont:
                break

            print(f"{Color.MAGENTA}[*] 조건검색 연속 조회 중... (누적 {len(results)}종목){Color.RESET}")

        if not results:
            print(f"{Color.RED}[!] 조건검색식({seq}) 결과가 없습니다. "
                  f"rt_cd={meta.get('rt_cd')} {meta.get('msg_cd', '')} {meta.get('msg1', '')}")
            print(f"{Color.RED}    → HTS에 조건검색식이 저장되어 있는지, KIS_ID가 HTS 아이디인지 확인하세요.{Color.RESET}")

        return results

    def fetch_psearch_stocks(self, seq=0):
        """조건검색 결과의 종목 코드만 반환 (거래대금 내림차순)"""
        return [item["code"] for item in self.fetch_psearch_detail(seq)]

    def fetch_top_by_amount(self, seq=0, top_n=30):
        """조건검색 결과 중 거래대금 상위 top_n개만 반환"""
        items = self.fetch_psearch_detail(seq)
        items.sort(key=lambda x: x.get("trade_amt", 0.0), reverse=True)
        return items[:top_n]