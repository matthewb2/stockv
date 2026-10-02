# -*- coding: utf-8 -*-
import os
import json
import argparse

def calculate_realized_win_rate(orders_file="orders.json"):
    """
    orders.json 파일에 이미 기록되어 있는 SELL(매도) 내역의 pnl_pct를 기반으로
    정확한 적중률(승률)과 총 손익을 계산합니다.
    """
    if not os.path.exists(orders_file):
        print(f"❌ '{orders_file}' 파일을 찾을 수 없습니다.")
        return

    try:
        with open(orders_file, "r", encoding="utf-8") as f:
            orders = json.load(f)
    except Exception as e:
        print(f"❌ 주문 내역 파일을 읽는 중 오류가 발생했습니다: {e}")
        return

    if not orders:
        print("⚠️ 기록된 주문 내역이 없습니다.")
        return

    wins = 0
    losses = 0
    total_trades = 0
    total_pnl_krw = 0.0

    #print(f"📊 총 {len(orders)}건의 주문 기록 중 완결된 매도(SELL) 내역 분석 ({orders_file})...\n")

    for idx, order in enumerate(orders, 1):
        order_type = order.get("type", "").upper()
        
        # SELL 기록만 대상으로 확정 수익 여부 판단
        if order_type == "SELL":
            market = order.get("market")
            pnl_pct = order.get("pnl_pct", 0.0)
            pnl_krw = order.get("pnl_krw", 0.0)
            timestamp = order.get("timestamp", "")
            
            total_trades += 1
            total_pnl_krw += pnl_krw

            # 0보다 크면 수익 (수수료 및 슬리피지 극복)
            if pnl_pct > 0:
                wins += 1
                result_str = f"🟢 수익 (수익률: {pnl_pct:+.2f}%, 손익: {pnl_krw:+,.0f}원)"
            else:
                losses += 1
                result_str = f"🔴 손실 (수익률: {pnl_pct:+.2f}%, 손익: {pnl_krw:+,.0f}원)"

            #print(f"[{total_trades}회차] {timestamp} | 종목: {market} | {result_str}")

    print("\n" + "="*50)
    print("📈 [확정 거래(SELL) 기준 성과 요약]")
    print(f" - 총 완료된 매매 횟수: {total_trades}회")
    print(f" - 수익 횟수: {wins}회")
    print(f" - 손실 횟수: {losses}회")
    print(f" - 총 누적 실현 손익: {total_pnl_krw:+,.0f}원")
    
    if total_trades > 0:
        win_rate = (wins / total_trades) * 100
        print(f" - 최종 적중률(승률): {win_rate:.2f}%")
    else:
        print(" - 적중률 계산 불가 (완료된 매도 기록 없음)")
    print("="*50)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="업비트 자동매매 주문 기록(JSON) 기반 성과 분석 스크립트")
    parser.add_argument(
        "orders_file", 
        nargs="?", 
        default="orders.json", 
        help="분석할 주문 내역 JSON 파일 경로 (기본값: orders.json)"
    )
    args = parser.parse_args()
    
    calculate_realized_win_rate(args.orders_file)