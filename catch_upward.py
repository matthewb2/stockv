# -*- coding: utf-8 -*-
"""
상승Catch 프로그램 — 호재 기반 매수 의견 도출

조건검색식으로 걸러진 종목마다
  1) 카카오 검색 API 로 뉴스(호재/악재)를 수집하고
  2) yfinance 로 최근 5거래일 주가를 가져와
  3) Groq 에 둘을 함께 질의해 매수(BUY) / 보류(HOLD) 의견을 받는다.

매수 의견에는 반드시 근거(관심 종목명 + 근거 뉴스 + 가격 흐름)를 구체적으로
쓰도록 프롬프트로 강제한다.

주의: 이 프로그램은 판단만 하고 주문은 내리지 않는다. (읽기 전용)

사용 예:
    python catch_upward.py                      # 코스피 조건검색식 2번 상위 10종목
    python catch_upward.py --seq 0 --max-codes 5 # 코스닥
"""
import argparse
import os

from dotenv import load_dotenv

from scripts.console_color import Color
from scripts.decider import (BUY_ACTION, GroqDecider, normalize_decision,
                             safe_json_loads)
from scripts.kakao_search import KakaoSearch
from scripts.notifier import build_notifier
from scripts.price_history import fetch_daily_prices, summarize_prices

load_dotenv()

PRICE_DAYS = 5        # 최근 N거래일 주가
NEWS_SIZE = 10        # 종목당 뉴스 수집 건수
MAX_NEWS_IN_PROMPT = 8   # 프롬프트에 넣을 최대 뉴스 수 (토큰 절약)

SYSTEM_PROMPT = (
    "당신은 한국 주식 호재 분석 전문가입니다. "
    "제공된 뉴스와 최근 주가 흐름을 근거로 매수(BUY) 또는 보류(HOLD) 의견을 냅니다.\n"
    "\n"
    "판단 기준:\n"
    "1. 주가를 상승시킬 구체적인 호재(계약, 실적 개선, 인증, 인수, 정부 지원, "
    "수주, 신규 제품 등)가 있으면 BUY.\n"
    "2. 호재가 없거나 이미 주가에 반영됐거나 악재(소송, 실손실, 유상증자, 리스크)가 "
    "있으면 HOLD.\n"
    "3. 반드시 제공된 뉴스에 근거한 판단을 할 것. 뉴스에 없는 내용은 지어내지 말 것.\n"
    "4. reason 은 구체적으로 쓸 것. 어떤 뉴스(제목 핵심 + 매체 + 날짜)가 어떤 방향의 "
    "영향을 주는지, 최근 주가 흐름과 어떻게 맞는지 명시한다.\n"
    "   '좋은 종목입니다', '주가가 상승할 전망' 같은 상투적인 표현은 금지.\n"
    "\n"
    "오직 아래 JSON만 출력하고 다른 설명은 하지 마십시오:\n"
    '{"action": "BUY" 또는 "HOLD", "reason": "구체적인 근거 2~4문장", '
    '"catalysts": ["호재 요약1", "..."], "risks": ["위험요소1", "..."]}'
)

# 시장별 조건검색식 기본 번호 (KOSPI=2번식, KOSDAQ=1번식)
MARKET_SEQ = {"KOSPI": 1, "KOSDAQ": 0}


# ==============================
# 🧰 LLM 준비
# ==============================
def build_llm(model=None):
    """GroqDecider 의 클라이언트 생성(키/모델/선택적 의존성 처리)을 재사용"""
    decider = GroqDecider(model=model)
    return decider.client, decider.model


# ==============================
# 📝 프롬프트 조립
# ==============================
def build_prompt(item, prices, news):
    """종목 정보 + 최근 주가 + 뉴스를 하나의 질의 문자열로 만든다"""
    code = item.get("code", "")
    name = item.get("name", "")
    price = item.get("price")

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
        "위 뉴스를 종합해 BUY 또는 HOLD 와 그 근거를 JSON 으로만 답하십시오.",
    ]
    return "\n".join(lines)


def ask_llm(client, model, prompt):
    """Groq 질의 → 정규화된 의견 dict

    반환: {"action","reason","catalysts","risks"}
    파싱 실패 시 action 은 HOLD 가 되고 reason 에 사유가 남는다.
    """
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
    )

    raw = safe_json_loads(response.choices[0].message.content)
    decision = normalize_decision(raw)          # {"action","reason"} 계약 보장

    # 선택 필드는 정규화 과정에서 버려지므로 따로 챙긴다
    decision["catalysts"] = _as_list(raw.get("catalysts"))
    decision["risks"] = _as_list(raw.get("risks"))
    return decision


def _as_list(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if value and str(value).strip():
        return [str(value).strip()]
    return []


# ==============================
# 🔍 종목 분석
# ==============================
def analyze_stock(item, kakao, client, model, market="KOSPI",
                  days=PRICE_DAYS, news_size=NEWS_SIZE):
    """종목 1건에 대해 뉴스 + 주가를 수집하고 Groq 의견을 받는다"""
    code = item.get("code", "")
    name = item.get("name", "")

    news = kakao.search_stock(name, code, size=news_size)
    prices = fetch_daily_prices(code, days=days, market=market)

    prompt = build_prompt(item, prices, news)
    decision = ask_llm(client, model, prompt)

    decision.update({
        "code": code,
        "name": name,
        "price": item.get("price"),
        "news_count": len(news),
        "days": len(prices),
        "prices": prices,
        "news": news,
    })
    return decision


# ==============================
# 📄 출력
# ==============================
def print_report(results):
    """ 종목별 의견 출력"""
    if not results:
        print(f"{Color.YELLOW}검토할 종목이 없습니다.{Color.RESET}")
        return

    buys = [r for r in results if r["action"] == BUY_ACTION]

    for r in results:
        tag = (f"{Color.RED}📈 BUY {Color.RESET}" if r["action"] == BUY_ACTION
               else f"{Color.CYAN}⏸️  HOLD{Color.RESET}")
        head = f"\n{tag} {r['name']}({r['code']})"
        if r["price"]:
            head += f"  현재가 {r['price']:,.0f}원"
        print(head)
        print(f"   뉴스 {r['news_count']}건 / 주가 {r['days']}거래일")
        print(f"   근거: {r['reason']}")

        if r["catalysts"]:
            print(f"   {Color.GREEN}호재: {', '.join(r['catalysts'])}{Color.RESET}")
        if r["risks"]:
            print(f"   {Color.YELLOW}위험: {', '.join(r['risks'])}{Color.RESET}")

    print(f"\n{'=' * 60}")
    print(f"검토 {len(results)}종목 중 매수(BUY) 의견 {len(buys)}종목")
    if buys:
        for r in buys:
            print(f"  - {r['name']}({r['code']})")


def build_summary_message(results):
    """Discord 요약 메시지"""
    buys = [r for r in results if r["action"] == BUY_ACTION]
    lines = [f"검토 {len(results)}종목 중 매수 의견 {len(buys)}종목", ""]

    for r in results:
        mark = "📈 BUY" if r["action"] == BUY_ACTION else "⏸️ HOLD"
        lines.append(f"{mark} {r['name']}({r['code']}) — {r['reason']}")

    return "\n".join(lines)


# ==============================
# 🎯 진입점
# ==============================
def parse_args(argv=None, market="KOSPI"):
    parser = argparse.ArgumentParser(
        description="조건검색 종목의 호재 뉴스 + 최근 5거래일 주가로 매수 의견 도출")
    parser.add_argument("--seq", type=int, default=None,
                        help=f"HTS 조건검색식 번호 (미지정 시 시장별 기본값: "
                             f"{MARKET_SEQ})")
    parser.add_argument("--max-codes", type=int, default=10,
                        help="검토할 종목 수")
    parser.add_argument("--days", type=int, default=PRICE_DAYS,
                        help=f"조회할 거래일 수 (기본 {PRICE_DAYS})")
    parser.add_argument("--news-size", type=int, default=NEWS_SIZE,
                        help=f"종목당 뉴스 수집 건수 (기본 {NEWS_SIZE})")
    parser.add_argument("--market", default=market,
                        choices=["KOSPI", "KOSDAQ"],
                        help="yfinance 접미사 결정 (KOSPI=.KS, KOSDAQ=.KQ)")
    parser.add_argument("--model", default=None,
                        help="Groq 모델명 (기본 LLM_MODEL 환경변수)")
    args = parser.parse_args(argv)

    # --seq 을 안 줬을 때만 시장별 기본 조건검색식을 적용한다.
    # (parse 전에 default 로 박아두면 --market 만 바꿀 때 seq 는 옛값으로 남는다)
    if args.seq is None:
        args.seq = MARKET_SEQ.get(args.market, 1)
    return args


def main(argv=None, market="KOSPI"):
    args = parse_args(argv, market=market)

    # trade_core 의 KIS 팩토리를 재사용한다 (토큰 공유·가상계좌 설정 포함)
    from trade_core import build_native, build_scanner

    native = build_native()
    scanner = build_scanner(native, os.getenv("KIS_ID"))
    kakao = KakaoSearch()
    client, model = build_llm(args.model)

    print(f"🔍 조건검색식 {args.seq} 상위 {args.max_codes}종목 수집 중...")
    items = scanner.fetch_top_by_amount(args.seq, top_n=args.max_codes)
    if not items:
        print("❌ 조건검색 결과가 없습니다.")
        return 1

    if not kakao.enabled:
        print(f"{Color.YELLOW}⚠️ KAKAO_API 키가 없습니다. 뉴스 없이 진행하면 "
              f"거의 전부 HOLD 가 됩니다.{Color.RESET}")

    results = []
    for i, item in enumerate(items, 1):
        print(f"\n[{i}/{len(items)}] {item.get('name')}({item.get('code')}) 분석 중...")
        try:
            results.append(analyze_stock(
                item, kakao, client, model,
                market=args.market, days=args.days, news_size=args.news_size))
        except Exception as e:
            # 종목 하나가 실패해도 나머지는 계속 판단한다
            print(f"{Color.RED}❌ {item.get('code')} 분석 실패: {e}{Color.RESET}")

    print_report(results)
    build_notifier().send("📈 상승Catch 매수 의견", build_summary_message(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())