# -*- coding: utf-8 -*-
"""뉴스 + Groq 매도/보유 의견 수집과 3시간 캐시(SQLite)

흐름: KakaoSearch 로 종목 뉴스 → yfinance 로 최근 N거래일 흐름 → Groq 에
      매도(SELL)/보유(HOLD) 의견을 물어 본다.

같은 종목은 3시간 안에 다시 묻지 않고 저장된 결과를 쓴다(과금·지연 절감).
거의 같은 뉴스를 매 3분씩 다시 보내는 일이 없도록 캐시가 먼저 간다.

⚠️ scripts.decider.normalize_decision 은 BUY 가 아닌 값을 전부 HOLD 로
   바꾸므로(decider.py:40) SELL/HOLD 분류에 쓰면 매도 의견이 사라진다.
   그래서 아래 normalize_sell_decision 을 따로 쓴다.

의견 파싱/호출에 실패하면 HOLD 로 처리한다 — 캐시하지 않으므로 다음
사이클에서 다시 시도하며, 실패했다고 보유분을 팔지는 않는다.
"""
import json
import os
from datetime import datetime, timedelta

from scripts.console_color import Color
from scripts.decider import GroqDecider, safe_json_loads
from scripts.kakao_search import KakaoSearch
from scripts.price_history import fetch_daily_prices, summarize_prices

SELL_ACTION = "SELL"
HOLD_ACTION = "HOLD"
CACHE_HOURS = 3        # 동일 종목 재질의 금지 구간 (시간)

PRICE_DAYS = 5         # 프롬프트에 넣을 최근 N거래일
NEWS_SIZE = 15         # 종목당 뉴스 수집 건수 (관련 없는 기사가 걸러지므로 여유)
MAX_NEWS_IN_PROMPT = 8  # 프롬프트에 넣을 최대 뉴스 수 (토큰 절약)

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "news_opinion.db")

SYSTEM_PROMPT = (
    "당신은 한국 주식 리스크 분석 전문가입니다. "
    "제공된 뉴스와 최근 주가 흐름을 근거로 매도(SELL) 또는 보유(HOLD) 의견을 냅니다.\n"
    "\n"
    "판단 기준:\n"
    "1. 주가를 떨어뜨릴 구체적인 악재(소송, 실손실, 유상증자, 감익, 매출 하락, "
    "규제, 해지, 리콜 등)가 있거나 이미 주가에 반영되지 않았으면 SELL.\n"
    "2. 악재가 없거나 뉴스가 없거나, 악재가 이미 주가에 반영됐으면 HOLD.\n"
    "3. 반드시 제공된 뉴스에 근거한 판단을 할 것. 뉴스에 없는 내용은 지어내지 말 것.\n"
    "4. reason 은 구체적으로 쓸 것. 어떤 뉴스(제목 핵심 + 매체 + 날짜)가 어떤 방향의 "
    "영향을 주는지, 최근 주가 흐름과 어떻게 맞는지 명시한다.\n"
    "   '좋은 종목입니다', '주가가 하락할 전망' 같은 상투적인 표현은 금지.\n"
    "\n"
    "오직 아래 JSON만 출력하고 다른 설명은 하지 마십시오:\n"
    '{"action": "SELL" 또는 "HOLD", "reason": "구체적인 근거 2~4문장", '
    '"catalysts": ["악재 요약1", "..."], "risks": ["위험요소1", "..."]}'
)


def normalize_sell_decision(raw):
    """LLM 응답 → {"action": "SELL"|"HOLD", "reason": str}

    action 은 대소문자·공백·한글 표기를 흡수해 SELL 로만 바꾼다.
    나머지 값(모호한 출력, 파싱 에러)은 전부 HOLD 다 — 보류는 항상 안전하다.
    """
    if not isinstance(raw, dict):
        return {"action": HOLD_ACTION, "reason": "JSON 파싱 에러"}

    action = str(raw.get("action", "")).strip().upper()
    if action in (SELL_ACTION, "매도"):
        action = SELL_ACTION
    else:
        action = HOLD_ACTION

    return {"action": action, "reason": str(raw.get("reason", "")).strip()}


def _as_list(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if value and str(value).strip():
        return [str(value).strip()]
    return []


# ==============================
# 🗄️ 의견 저장소 (3시간 캐시)
# ==============================
class OpinionStore:
    """Groq 의견 보관소.

    로컬 시간 문자열(YYYY-MM-DD HH:MM:SS)을 쓰므로 정렬·기간 비교가
    사전순으로 성립한다. UTC 와 섞이지 않도록 조회 시각도 반드시
    datetime.now() 기준으로 계산한다.
    """
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS groq_opinions (
        ticker     TEXT NOT NULL,
        created_at TEXT NOT NULL,
        action     TEXT NOT NULL,
        reason     TEXT,
        news_count INTEGER,
        price      REAL,
        PRIMARY KEY (ticker, created_at)
    );
    CREATE INDEX IF NOT EXISTS idx_groopinion_ticker_time
        ON groq_opinions(ticker, created_at);
    """

    def __init__(self, db_file=DEFAULT_DB):
        import sqlite3

        self.db_file = db_file
        parent = os.path.dirname(os.path.abspath(db_file))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)

        self.conn = sqlite3.connect(db_file)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(self.SCHEMA)

    def get_fresh(self, ticker, hours=CACHE_HOURS):
        """최근 hours 시간 내 저장된 의견이 있으면 반환, 없으면 None

        return: {"ticker","created_at","action","reason","news_count","price"}
                없으면 None (== Groq 에 새로 물어야 한다)
        """
        cutoff = (datetime.now() - timedelta(hours=hours))
        cutoff = cutoff.strftime("%Y-%m-%d %H:%M:%S")

        row = self.conn.execute(
            "SELECT ticker, created_at, action, reason, news_count, price "
            "FROM groq_opinions "
            "WHERE ticker = ? AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT 1",
            (str(ticker), cutoff),
        ).fetchone()
        return dict(row) if row else None

    def save(self, ticker, action, reason="", news_count=0, price=None,
             created_at=None):
        """의견을 저장. 같은 종목·같은 초의 중복 기록은 덮어쓴다."""
        created_at = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "INSERT OR REPLACE INTO groq_opinions "
            "(ticker, created_at, action, reason, news_count, price) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(ticker), created_at, action, reason, int(news_count or 0),
             price),
        )
        self.conn.commit()
        return created_at

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None


# ==============================
# 📝 프롬프트 조립
# ==============================
_SEARCH_TRIM_SUFFIXES = ("지주", "홀딩스", "그룹")


def search_name(name):
    """검색용 종목명 — 'KB금융지주' → 'KB금융'

    Kakao 웹 검색은 정식 명칭을 그대로 넣으면 뉴스를 거의 못 돌려주고
    축약형이 훨씬 잘 먹힌다.
    """
    name = str(name or "").strip()
    for suffix in _SEARCH_TRIM_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)].strip()
    return name


def _is_relevant(article, names, code):
    """제목에 종목명(들) 또는 코드가 없는 뉴스는 제외한다

    웹 검색은 타 종목 주가 페이지·포럼 글을 자주 섞는다. 그걸 그대로
    Groq 에 주면 '악재 없음' 판단을 흐린다.
    """
    title = str(article.get("title") or "")
    for name in names:
        if name and str(name) in title:
            return True
    if code and str(code) in title:
        return True
    return False


def build_prompt(name, code, price, prices, news):
    """종목 정보 + 최근 주가 + 뉴스를 하나의 질의 문자열로 만든다"""
    lines = [
        f"종목: {name}({code})",
        f"현재가: {price:,.0f}원" if price else "현재가: 조회 실패",
        f"최근 {len(prices)}거래일 흐름:",
        summarize_prices(prices),
        "",
        f"검색된 뉴스 {len(news)}건:",
    ]

    if news:
        for n in news[:MAX_NEWS_IN_PROMPT]:
            when = f" ({n['date']})" if n.get("date") else ""
            press = f"[{n['press']}]" if n.get("press") else ""
            lines.append(f"- {n['title']}{when} {press}")
            if n.get("summary"):
                lines.append(f"    요약: {n['summary']}")
    else:
        lines.append("- (검색된 뉴스가 없습니다)")

    lines += [
        "",
        "위 뉴스를 종합해 SELL 또는 HOLD 와 그 근거를 JSON 으로만 답하십시오.",
    ]
    return "\n".join(lines)


# ==============================
# 🤖 뉴스 기반 의견 조회
# ==============================
class NewsAdvisor:
    """캐시 우선으로 종목의 매도/보유 의견을 돌려준다.

    kakao / groq 는 None 이면 데모·오프라인 모드로 동작한다
    (kakao 없이는 뉴스 0건, groq 없이는 HOLD 로 떨어진다).
    """

    def __init__(self, store, kakao=None, model=None):
        self.store = store
        self.kakao = kakao
        self.model = model
        self._client = None
        self._client_error = None

    # --- Groq 클라이언트 (한 번만 만든다) ---
    @property
    def client(self):
        if self._client is None and self._client_error is None:
            try:
                self._client = GroqDecider(model=self.model)
            except Exception as e:
                self._client_error = str(e)
                print(f"{Color.YELLOW}⚠️ Groq 사용 불가 → 의견은 HOLD 로 "
                      f"처리합니다: {e}{Color.RESET}")
        return self._client

    def _query(self, prompt):
        """Groq 질의 → {"action","reason"}. 실패 시 HOLD"""
        decider = self.client
        if decider is None:
            return {"action": HOLD_ACTION, "reason": "Groq 사용 불가"}

        try:
            response = decider.client.chat.completions.create(
                model=decider.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
            )
            raw = safe_json_loads(response.choices[0].message.content)
        except Exception as e:
            print(f"{Color.YELLOW}⚠️ Groq 질의 실패 → HOLD 로 처리: {e}"
                  f"{Color.RESET}")
            return {"action": HOLD_ACTION, "reason": f"Groq 질의 실패: {e}"}

        decision = normalize_sell_decision(raw)
        decision["catalysts"] = _as_list(raw.get("catalysts")) if isinstance(raw, dict) else []
        decision["risks"] = _as_list(raw.get("risks")) if isinstance(raw, dict) else []
        return decision

    def opinion(self, ticker, name=None, price=None, market="KOSPI"):
        """종목 의견 — 3시간 내 저장분이 있으면 Groq 에 물리지 않는다.

        return: {"ticker","name","action","reason","news_count",
                 "source","created_at"}  source = "cache" | "live" | "error"
        """
        ticker = str(ticker)
        name = name or ticker

        cached = self.store.get_fresh(ticker, hours=CACHE_HOURS)
        if cached:
            return {
                "ticker": ticker,
                "name": name,
                "action": cached["action"],
                "reason": cached.get("reason") or "",
                "news_count": cached.get("news_count") or 0,
                "source": "cache",
                "created_at": cached["created_at"],
            }

        # 캐시가 없을 때만 뉴스·주가·Groq 를 순서대로 돈다
        if self.kakao is None:
            print(f"{Color.YELLOW}⚠️ [{ticker}] KakaoSearch 미지정 → "
                  f"뉴스 0건으로 질의합니다{Color.RESET}")
            news = []
        else:
            short = search_name(name)
            query_name = short or name
            names = [name] if short == name else [name, short]
            raw = self.kakao.search_stock(query_name, ticker, size=NEWS_SIZE)
            news = [n for n in raw if _is_relevant(n, names, ticker)]
        try:
            prices = fetch_daily_prices(ticker, days=PRICE_DAYS, market=market)
        except Exception as e:
            print(f"{Color.YELLOW}⚠️ 일봉 조회 실패({ticker}): {e}{Color.RESET}")
            prices = []

        prompt = build_prompt(name, ticker, price, prices, news)
        decision = self._query(prompt)

        # 캐시는 성공분만 남긴다 (실패 HOLD 는 매 사이클 재시도 대상)
        usable = not str(decision.get("reason", "")).startswith(
            ("Groq 질의 실패", "JSON 파싱"))
        created_at = None
        if usable:
            created_at = self.store.save(
                ticker, decision["action"], decision.get("reason", ""),
                news_count=len(news), price=price)

        return {
            "ticker": ticker,
            "name": name,
            "action": decision["action"],
            "reason": decision.get("reason", ""),
            "news_count": len(news),
            "source": "live" if usable else "error",
            "created_at": created_at,
        }


def format_opinion(decision):
    """의견 dict 를 한 줄 로그용 문자열로"""
    if not decision:
        return "의견 없음"
    source = {"cache": "캐시", "live": "신규", "error": "에러"}.get(
        decision.get("source"), decision.get("source", ""))
    return (f"{decision.get('action', '?')} ({source}, "
            f"뉴스 {decision.get('news_count', 0)}건)")
