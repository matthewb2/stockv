import requests
import time

# 장 시작/종료 (3분봉 paging 범위 계산용)
MARKET_OPEN_MIN = 9 * 60       # 09:00
MARKET_CLOSE_MIN = 20 * 60     # 20:00 (장 마감 포함 최신 봉)


class KISNative:
    BASE_URL_REAL = "https://openapi.koreainvestment.com:9443"
    BASE_URL_VTS = "https://openapivts.koreainvestment.com:29443"

    def __init__(self, app_key, app_secret, account_prefix, virtual=False):
        """
        Args:
            app_key / app_secret: openapi.koreainvestment.com 용 앱키 (실전)
            account_prefix: 계좌 앞 8자리
            virtual: True면 한투 모의계좌 서버로 접속 (기본값은 실전 계좌)
        """
        self.app_key = app_key
        self.app_secret = app_secret
        self.account_prefix = account_prefix
        self.account_suffix = "01"

        self.virtual = virtual

        # 실전/모의에 따라 서버·토큰·잔고조회 TR 코드가 달라진다.
        self.base_url = self.BASE_URL_VTS if virtual else self.BASE_URL_REAL
        self.token_path = "/oauth2/tokenP" if virtual else "/oauth2/token"
        self.balance_tr_id = "VTTC8434R" if virtual else "TTTC8434R"

        self.access_token = None
        self.token_expire = 0

    def get_balance(self):
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._get_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": self.balance_tr_id,
            "custtype": "P"
        }
        params = {
            "CANO": self.account_prefix, "ACNT_PRDT_CD": self.account_suffix,
            "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
            "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""
        }
        res = requests.get(url, headers=headers, params=params)
        data = res.json()
        return data.get('output1', []) if data.get('rt_cd') == '0' else []

    def get_holding_codes(self):
        """보유 종목 코드 목록 (중복 매수 방지용)"""
        codes = []

        for row in self.get_balance():
            qty = int(row.get('hldg_qty', 0) or 0)
            code = row.get('prdt_cd')
            if code and qty > 0:
                codes.append(code)

        return codes

    def _get_token(self):
        now = time.time()

        if self.access_token and now < self.token_expire:
            return self.access_token

        url = f"{self.base_url}{self.token_path}"

        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }

        res = requests.post(url, data=body, timeout=10)
        res.raise_for_status()

        data = res.json()

        self.access_token = data["access_token"]

        # 대충 23시간 캐시
        self.token_expire = now + 82800

        return self.access_token

    def get_access_token(self):
        """유효한 액세스 토큰 반환 (다른 모듈에서 공유 가능)"""
        return self._get_token()

    def get_name(self, code):
        """
        종목코드로 종목명 조회

        code: 종목코드 (예: 005930)

        return:
            str
        """

        token = self._get_token()

        url = (
            f"{self.base_url}"
            "/uapi/domestic-stock/v1/quotations/search-info"
        )

        headers = {
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": "CTPF1604R",
        }

        params = {
            "PDNO": code,
            "PRDT_TYPE_CD": "300",
        }

        res = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=5,
        )

        res.raise_for_status()

        data = res.json()

        if data["rt_cd"] != "0":
            raise RuntimeError(data["msg1"])

        return data["output"]["prdt_name"]
    
    def get_price(self, code):
        """
        현재가 조회

        code: 종목코드 (예: 005930)

        return:
            {
                "code": str,
                "name": str,
                "price": int,
                "open": int,
                "high": int,
                "low": int,
                "volume": int,
            }
        """

        token = self._get_token()

        url = (
            f"{self.base_url}"
            "/uapi/domestic-stock/v1/quotations/inquire-price"
        )

        headers = {
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,

            # 모의/실전 동일
            "tr_id": "FHKST01010100",
        }

        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
        }

        res = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=5,
        )

        res.raise_for_status()

        data = res.json()["output"]

        return {
            "code": code,
            "price": int(data["stck_prpr"]),
            "open": int(data["stck_oprc"]),
            "high": int(data["stck_hgpr"]),
            "low": int(data["stck_lwpr"]),
            "volume": int(data["acml_vol"]),
        }
    
    def get_3m_chart(self, code, hour=""):
        """
        code: 종목코드 (예: 005930)
        hour: "HHMMSS" 지정 시 해당 시각까지의 과거 구간 30개 반환 (기본값 "" = 현재)

        return:
            list[dict]
        """

        token = self._get_token()

        url = (
            f"{self.base_url}"
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
        )

        headers = {
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,

            # 실전계좌 TR
            "tr_id": "FHKST03010200",
        }

        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,

            # 현재시각부터 역순 (HHMMSS 지정 시 해당 구간)
            "FID_INPUT_HOUR_1": hour,

            # 3분봉
            "FID_PERIOD_DIV_CODE": "3",

            "FID_PW_DATA_INCU_YN": "Y",
        }

        res = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=5,
        )

        res.raise_for_status()

        data = res.json()

        if data.get("rt_cd") != "0":
            return []

        rows = []

        for item in data.get("output2") or []:
            rows.append(
                {
                    "time": item["stck_cntg_hour"],
                    "open": int(item["stck_oprc"]),
                    "high": int(item["stck_hgpr"]),
                    "low": int(item["stck_lwpr"]),
                    "close": int(item["stck_prpr"]),
                    "volume": int(item["cntg_vol"]),
                }
            )

        return rows

    def get_3m_chart_history(self, code, min_bars=150, step_minutes=30):
        """과거 구간을 분할 조회해 당일 3분봉을 min_bars개 이상 확보한다.

        1회 호출은 30개로 제한되지만, FID_INPUT_HOUR_1을 과거로 지정하면
        다른 구간 30개를 받아올 수 있다. 09:00부터 현재까지 paging한다.

        KIS 3분봉 응답에는 장 시간대 밖의 거래량 0 채움 봉이 섞여 있으므로
        부족 여부는 거래량이 있는 봉만으로 판단한다. 채움 봉까지 세면
        필요한 봉을 확보하지 못한 채 paging이 조기에 종료된다.

        return:
            (list[dict] 정방향, 실제 확보 봉 수)
        """
        collected = {}

        # 09:00 이후 미래 시각만 필요하므로 장 마감(20:00)부터 역순으로 paging
        hours = [""]
        total_minutes = (MARKET_CLOSE_MIN - MARKET_OPEN_MIN)
        elapsed = 0
        while elapsed < total_minutes:
            elapsed += step_minutes
            minutes = MARKET_CLOSE_MIN - elapsed
            if minutes < MARKET_OPEN_MIN:
                break
            hours.append(f"{minutes // 60:02d}{minutes % 60:02d}00")

        for hour in hours:
            for bar in self.get_3m_chart(code, hour=hour):
                collected.setdefault(bar["time"], bar)

            valid = sum(1 for b in collected.values() if b.get("volume", 0) > 0)
            if valid >= min_bars and hour:
                # 유효 봉이 충분히 모였으면 더 내려갈 필요 없음
                break

            time.sleep(0.2)

        rows = [collected[t] for t in sorted(collected)]
        return rows, len(rows)