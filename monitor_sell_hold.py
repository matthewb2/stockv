#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""보유종목 하락 매도 봇 — 3분 주기 루프 + macd_hmm 국면 + Groq 뉴스 의견

매 사이클, 보유 종목마다 아래를 순서대로 한다.

  1. 최신 3분봉을 KIS API 로 받아 macd_hmm 로 상승/횡보/하락 판정
  2. Kakao 뉴스 + 최근 주가를 Groq 에 올려 매도(SELL)/보유(HOLD) 의견 확인
     — 같은 종목은 3시간 안에 다시 묻지 않고 SQLite 에 저장된 결과를 쓴다
        (news_opinion.db, groq_opinions 테이블)
  3. **국면 하락 AND Groq 매도** 일 때만 전량 시장가 매도
     — 국면 하락이어도 AI 가 보유로 보면 팔지 않는다.
       상승·횡보·판별 보류는 매매하지 않는다. 매수는 하지 않는다.
  4. 매도가 발생하면 Discord 알림 채널로 결과(수량·가격·국면·AI 근거·손익) 보고

라벨은 종목 자신에 대한 상대 위치이므로 "하락" 은 '상대 약화(모멘텀 둔화)'
를 뜻하며 가격이 오르는 중에도 나올 수 있다.

주문/잔고는 로컬 모의매매(KisPaperTrader)가 갱신하는
kospi_balance.json / kospi_orders.json 을 쓴다 (직접 편집 금지).

사용법:
    python monitor_sell_hold.py                 # 3분 주기 무한 루프
    python monitor_sell_hold.py --max-cycles 1  # 1회만 실행 (점검용)
    python monitor_sell_hold.py --dry-run       # 판정만 출력, 주문 없음
    python monitor_sell_hold.py --interval 60   # 주기(초) 변경

환경변수: KIS_APPKEY / KIS_SECRETKEY / KIS_ACCOUNT
         GROQ_API_KEY / LLM_MODEL / KAKAO_API / DISCORD_WEBHOOK_STOCK_URL
"""
import argparse
import json
import os
import re
import sys
from time import sleep

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from kis_trader import KisPaperTrader  # noqa: E402
from market_regime import (  # noqa: E402
    MODEL_MACD_HMM,
    REGIME_DOWNTREND,
    analyze_stock_regime,
)
from scripts.chart_store import open_store  # noqa: E402
from scripts.console_color import Color  # noqa: E402
from scripts.kakao_search import KakaoSearch  # noqa: E402
from scripts.news_advisor import (  # noqa: E402
    CACHE_HOURS,
    DEFAULT_DB as DEFAULT_OPINION_DB,
    HOLD_ACTION,
    SELL_ACTION,
    NewsAdvisor,
    OpinionStore,
    format_opinion,
)
from scripts.notifier import build_notifier  # noqa: E402
from trade_core import (  # noqa: E402
    POLLING_INTERVAL,
    TradingContext,
    build_native,
    fetch_stock_3m_data,
    get_current_price,
    get_stock_market,
    min_training_bars,
)

BALANCE_FILE = os.path.join(BASE_DIR, "kospi_balance.json")
ORDERS_FILE = os.path.join(BASE_DIR, "kospi_orders.json")
DB_FILE = os.path.join(BASE_DIR, "kospi_chart.db")

# 판별 결과 → 표기 (None 은 판별 보류)
REGIME_DISPLAY = {
    "Uptrend": "📈 상승",
    "Sideways": "➡️ 횡보",
    "Downtrend": "📉 하락",
    None: "⏸️ 판별 보류",
}
REGIME_ORDER = {"Uptrend": 0, "Sideways": 1, "Downtrend": 2, None: 3}


def collect_holdings(trader):
    """모의매매 잔고 파일에서 최신 상태를 동기화하여 수량이 0보다 큰 종목 전체를 반환"""
    balance_file = getattr(trader, "balance_file", None)
    if balance_file and os.path.exists(balance_file):
        try:
            with open(balance_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    if "balances" in data:
                        trader.balances = data["balances"]
                    if "avg_buy_price" in data:
                        trader.avg_buy_price = data["avg_buy_price"]
                    elif not any(k in data for k in ["balances", "avg_buy_price"]):
                        trader.balances = data
        except Exception:
            pass

    holdings = []
    for ticker, qty in sorted(trader.balances.items()):
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        avg = trader.avg_buy_price.get(ticker) or 0.0
        try:
            avg = float(avg)
        except (TypeError, ValueError):
            avg = 0.0
        holdings.append((str(ticker), qty, avg))
    return holdings


def _fmt_price(value):
    if not value:
        return "-"
    return f"{value:,.0f}"


def _fmt_return(avg, price):
    if not avg or avg <= 0 or price is None:
        return "-"
    pct = (price - avg) / avg * 100.0
    return f"{'+' if pct >= 0 else ''}{pct:.2f}%"


_NAME_SUFFIX_RE = re.compile(r"\s*\d*(?:보통주|우선주|우B)\s*$")


def clean_name(name):
    """KIS 종목명의 '보통주/우선주/1우/우B' 접미사를 뗀다"""
    original = str(name or "").strip()
    cleaned = _NAME_SUFFIX_RE.sub("", original).strip()
    return cleaned or original


def resolve_name(ctx, ticker):
    """보유종목의 정식 종목명을 KIS 에서 한 번만 받아 name_cache 에 넣는다"""
    if ticker in ctx.name_cache:
        return ctx.name_cache[ticker]
    try:
        name = ctx.native.get_name(ticker)
    except Exception:
        name = None
    name = clean_name(name)
    if name:
        ctx.name_cache[ticker] = name
    return ctx.stock_name(ticker)


def sell_holding(ctx, ticker, price, name, regime, decision):
    """전량 시장가 매도 후 Discord 로 매매 결과를 보고한다"""
    if ctx.paper.get_balance(ticker) <= 0:
        return False

    res = ctx.paper.sell_market_order(ticker, price)
    if not res:
        return False

    msg = (
        f"📉 **[하락 매도]**\n"
        f"종목: {name}({ticker}) {res['qty']}주 @ {price:,.0f}원\n"
        f"국면: {REGIME_DISPLAY.get(regime, regime)} [{MODEL_MACD_HMM}]\n"
        f"AI 의견: {format_opinion(decision)}\n"
        f"근거: {decision.get('reason', '') or '-'}\n"
        f"손익: {res['pnl_pct']:+.2f}% ({res['pnl_krw']:,.0f}원)"
    )
    ctx.notifier.send("PAPER SELL", msg)
    return True


def analyze_holdings(ctx, advisor, dry_run=False):
    """보유 종목 전체를 한 종목씩 판정하며, 개별 종목 네트워크 에러 시 건너뛰고 계속 진행한다"""
    results = []
    holdings = collect_holdings(ctx.paper)
    if not holdings:
        print(f"{Color.YELLOW}⏸️ 보유 종목이 없습니다. 매도 대상 없음.{Color.RESET}")
        return results

    for idx, (ticker, qty, avg) in enumerate(holdings, start=1):
        name = resolve_name(ctx, ticker)
        print(f"\n{'-' * 60} ({idx}/{len(holdings)})")

        try:
            # 최신 3분봉 조회 → 로컬 DB 누적 → 반환
            df = fetch_stock_3m_data(ctx.native, ticker, ctx.store,
                                     min_bars=min_training_bars())
            if df is None or df.empty:
                print(f"{Color.YELLOW}⚠️ [{name}({ticker})] 3분봉 조회 실패 → "
                      f"판별 불가{Color.RESET}")
                results.append(dict(ticker=ticker, qty=qty, avg=avg, price=None,
                                    return_text="-", regime=None, opinion=None,
                                    sold=False))
                continue

            price = get_current_price(ctx.native, ticker)
            regime = analyze_stock_regime(ticker, df, model=MODEL_MACD_HMM)

            # 뉴스 + Groq 의견 (3시간 내 저장분 사용)
            decision = advisor.opinion(
                ticker, name=name, price=price,
                market=get_stock_market(ticker, warn=False))

            reason = decision.get("reason") or ""
            print(f"   {ticker} {name}  {qty:>4}주  평단 {_fmt_price(avg):>9}  "
                  f"현재 {_fmt_price(price):>9}  {_fmt_return(avg, price):>8}"
                  f"   → {REGIME_DISPLAY.get(regime, regime)}")
            print(f"   🤖 Groq {format_opinion(decision)}"
                  + (f": {reason[:110]}" if reason else ""))

            is_down = regime == REGIME_DOWNTREND
            is_sell = decision.get("action") == SELL_ACTION

            sold = False
            if dry_run:
                if is_down and is_sell:
                    print(f"   {Color.CYAN}🔎 --dry-run: 하락 + 매도 의견 → 매도 생략{Color.RESET}")
                elif is_down:
                    print(f"   {Color.CYAN}⏸️ 하락이지만 AI 보유({HOLD_ACTION}) 의견 → 매도 보류{Color.RESET}")
            elif is_down and is_sell:
                sold = sell_holding(ctx, ticker, price, name, regime, decision)
                if sold:
                    print(f"   {Color.RED}🔻 [{name}({ticker})] 전량 매도 완료{Color.RESET}")
            elif is_down:
                print(f"   {Color.YELLOW}⏸️ 하락이지만 AI 보유({HOLD_ACTION}) 의견 → 매도 보류{Color.RESET}")

            results.append(dict(ticker=ticker, qty=qty, avg=avg, price=price,
                                return_text=_fmt_return(avg, price),
                                regime=regime, opinion=decision, sold=sold))

        except Exception as err:
            # 개별 종목 조회 중 타임아웃/네트워크 오류 발생 시 다음 종목으로 넘어감
            print(f"{Color.RED}❌ [{name}({ticker})] 스캔 중 에러 발생: {err}{Color.RESET}")
            results.append(dict(ticker=ticker, qty=qty, avg=avg, price=None,
                                return_text="-", regime=None, opinion=None,
                                sold=False))
            sleep(1)  # 요청 과부하 방지용 짧은 대기

    return results


def print_summary(ctx, results):
    """최종 요약표 — 국면 순 정렬, AI 의견과 매도 건을 표시"""
    if not results:
        return

    print()
    print("=" * 84)
    print(f"{'종목':<9}{'보유':>6}{'평단':>11}{'현재':>11}{'수익률':>10}"
          f"{'국면':>14}{'AI':>6}{'주문':>8}")
    print("-" * 84)
    for r in sorted(results, key=lambda x: REGIME_ORDER.get(x["regime"], 3)):
        op = r.get("opinion")
        name = resolve_name(ctx, r['ticker'])
        op_text = op.get("action", "-") if op else "-"
        print(f"{r['ticker']:<7}{name:<14}{r['qty']:>6}"
              f"{_fmt_price(r['avg']):>10}{_fmt_price(r['price']):>10}"
              f"{r['return_text']:>9}"
              f"{REGIME_DISPLAY.get(r['regime'], str(r['regime'])):>11}"
              f"{op_text:>6}"
              f"{'매도' if r['sold'] else '-':>8}")
    print("=" * 84)

    counts = {}
    for r in results:
        counts[r["regime"]] = counts.get(r["regime"], 0) + 1
    parts = [f"{REGIME_DISPLAY.get(k, 'None')} {v}"
             for k, v in sorted(counts.items(), key=lambda kv: REGIME_ORDER.get(kv[0], 3))]
    opinions = {}
    for r in results:
        op = r.get("opinion")
        key = op.get("action") if op else None
        opinions[key] = opinions.get(key, 0) + 1
    ai_parts = [f"{k} {v}" for k, v in sorted(opinions.items(), key=lambda kv: str(kv[0]))]
    sold_n = sum(1 for r in results if r["sold"])
    print(f"합계 {len(results)}종목 — " + " / ".join(parts))
    print(f"     AI: " + " / ".join(ai_parts) + f"   (매도 {sold_n}건)")


def build_context(args):
    """TradingContext 구성"""
    native = build_native()
    return TradingContext(
        native=native,
        store=open_store(args.store_file),
        paper=KisPaperTrader(initial_krw=args.initial_krw,
                             balance_file=args.balance_file,
                             orders_file=args.orders_file),
        notifier=build_notifier(),
        market="KOSPI",
    )


def run_loop(ctx, advisor, args):
    header = "=" * 84
    print(header)
    print(f"📉 보유종목 하락 매도 봇 — {MODEL_MACD_HMM} + Groq 뉴스 의견")
    print(f"   주기 {args.interval}초 / 잔고 {os.path.basename(args.balance_file)}"
          f" / 주문 {os.path.basename(args.orders_file)}")
    print(f"   분봉 {os.path.basename(args.store_file)} (KIS API 로 최신 봉 누적)")
    print(f"   의견DB {os.path.basename(advisor.store.db_file)} "
          f"({CACHE_HOURS}시간 내 저장분 재사용)")
    print(f"   매도 조건: 국면 하락 AND Groq 매도. 그 외는 매매 없음."
          f"{'   [dry-run]' if args.dry_run else ''}")
    print(header)
    ctx.paper.get_status()

    summary = ctx.store.summary()
    if summary:
        print(f"\n📊 분봉 누적 현황 (학습 {min_training_bars()}봉 필요):")
        for ticker, count, updated in summary:
            print(f"   - {ticker}: {count}봉 (최근 갱신 {updated})")

    cycle = 0
    while args.max_cycles is None or cycle < args.max_cycles:
        cycle += 1
        print(f"\n{Color.CYAN}──────────── 보유종목 점검 {cycle} ────────────{Color.RESET}")

        try:
            results = analyze_holdings(ctx, advisor, dry_run=args.dry_run)
            print_summary(ctx, results)
        except Exception as e:
            print(f"{Color.RED}❌ 루프 에러: {e}{Color.RESET}")

        if args.max_cycles is not None and cycle >= args.max_cycles:
            print(f"\n{Color.CYAN}⏹️ {args.max_cycles}회 실행 후 종료합니다.{Color.RESET}")
            break

        print(f"\n{Color.CYAN}⏳ 다음 점검까지 {args.interval}초 대기...{Color.RESET}")
        sleep(args.interval)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="보유종목 3분 주기 점검 — macd_hmm 하락 시 전량 매도")
    parser.add_argument("--interval", type=int, default=POLLING_INTERVAL,
                        help="점검 주기(초) (기본 180 = 3분)")
    parser.add_argument("--max-cycles", type=int, default=None,
                        help="점검 횟수 제한 (테스트 종료용)")
    parser.add_argument("--dry-run", action="store_true",
                        help="판정만 출력하고 주문을 넣지 않는다")
    parser.add_argument("--initial-krw", type=float, default=10_000_000,
                        help="잔고 파일이 없을 때의 초기 원금")

    paper = parser.add_argument_group("로컬 모의매매 옵션")
    paper.add_argument("--balance-file", default=BALANCE_FILE, help="잔고 JSON 경로")
    paper.add_argument("--orders-file", default=ORDERS_FILE, help="주문 JSON 경로")

    store = parser.add_argument_group("분봉 누적 저장소 옵션")
    store.add_argument("--store-file", default=DB_FILE, help="3분봉 DB 경로")

    news = parser.add_argument_group("뉴스/Groq 의견 옵션")
    news.add_argument("--opinion-db", default=None, help="의견 저장 SQLite 경로 (기본: news_opinion.db)")

    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        ctx = build_context(args)
    except SystemExit as exc:
        print(exc)
        return 1

    advisor_store = OpinionStore(args.opinion_db or DEFAULT_OPINION_DB)
    advisor = NewsAdvisor(advisor_store, kakao=KakaoSearch())

    try:
        run_loop(ctx, advisor, args)
    except KeyboardInterrupt:
        print("\n[운영 중단] 점검 루프를 종료합니다.")
    finally:
        advisor_store.close()
        ctx.store.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())