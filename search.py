# -*- coding: utf-8 -*-
"""
HTS 조건검색식 번호·내용 확인 도구.

KIS 조건검색 API(psearch-result)는 '저장된 조건식 목록'이나 '조건식 이름'을
돌려주지 않는다. seq 번호 → 해당 조건식의 현재 결과 종목만 조회된다.
따라서 이 도구는 seq 를 순회하면서 각 조건식의 상태 / 종목 수 / 시장 구성 /
거래대금 상위 종목을 출력해 "어떤 번호가 어떤 조건식인지" 확인하게 해준다.

  # 실전 계정 seq 0~19 훑기
  python search.py

  # 특정 seq 상세 (상위 20종목)
  python search.py --seq 0 --limit 20

  # 구간 지정
  python search.py --start 0 --end 49

  # 모의계정
  python search.py --account vts

  # 시장 분류 없이 종목만 확인
  python search.py --no-market

  # 결과 저장
  python search.py --json conditions.json
"""
import argparse
import json
import os
import sys
import unicodedata

import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Windows 콘솔(cp949)에서 한글/이모지 출력이 깨지지 않도록 UTF-8 로 재설정
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

PATH_PSEARCH = "/uapi/domestic-stock/v1/quotations/psearch-result"
TR_ID_PSEARCH = "HHKST03900400"
MAX_PAGES = 20              # 연속조회 안전 장치

ACCOUNTS = {
    "real": {
        "label": "실전",
        "base_url": "https://openapi.koreainvestment.com:9443",
        "token_path": "/oauth2/token",
        "app_key": "KIS_APPKEY",
        "app_secret": "KIS_SECRETKEY",
        "user_id": "KIS_ID",
    },
    "vts": {
        "label": "모의",
        "base_url": "https://openapivts.koreainvestment.com:29443",
        "token_path": "/oauth2/tokenP",
        "app_key": "KIS_VIRTUAL_APPKEY",
        "app_secret": "KIS_VIRTUAL_SECRETKEY",
        "user_id": "KIS_VIRTUAL_ID",
    },
}

# KRX 정보데이터 시세 파일(cp949). 각 줄의 첫 필드가 종목코드다.
MARKET_FILES = {
    "KOSPI": "kospi.txt",
    "KOSDAQ": "kosdaq.txt",
}

MISSING = "미상"


# ==============================
# 🖨️ 출력 정렬
# ==============================
def display_width(text):
    """한글/이모지 등 전각 문자는 2칸으로 계산"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in str(text))


def pad(text, width, align="left"):
    """전각 문자 폭을 반영한 고정폭 패딩"""
    text = str(text)
    gap = max(0, width - display_width(text))
    return text + " " * gap if align == "left" else " " * gap + text


# ==============================
# 📋 시장 목록
# ==============================
def load_market_codes():
    """kospi.txt / kosdaq.txt 에서 종목코드 집합 로드"""
    codes = {}
    for market, fname in MARKET_FILES.items():
        path = os.path.join(BASE_DIR, fname)
        found = set()
        if os.path.exists(path):
            with open(path, encoding="cp949", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("^"):
                        continue
                    code = line.split(",")[0].strip()
                    if code:
                        found.add(code.upper())
        codes[market] = found
    return codes


def classify(code, codes):
    """소속 시장 판별 (코스닥 우선, 그 외 코스피, 미상)"""
    code = str(code).strip().upper()
    if code in codes.get("KOSDAQ", ()):
        return "KOSDAQ"
    if code in codes.get("KOSPI", ()):
        return "KOSPI"
    return MISSING


# ==============================
# 🔑 인증 및 조회
# ==============================
def issue_token(cfg):
    """액세스 토큰 발급"""
    res = requests.post(f"{cfg['base_url']}{cfg['token_path']}",
                        data={"grant_type": "client_credentials",
                              "appkey": cfg["app_key"],
                              "appsecret": cfg["app_secret"]},
                        timeout=10)
    try:
        token = res.json().get("access_token")
    except ValueError:
        token = None

    if not token:
        raise SystemExit(f"❌ 토큰 발급 실패: {res.status_code} {res.text[:200]}")
    return token


def _to_float(value):
    """KIS 응답 수치는 ' 3970121479.1210' 처럼 문자열 + 공백이 섞여 있다"""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def fetch_condition(cfg, token, seq):
    """seq 조건식의 전체 결과(연속조회 포함)를 반환"""
    headers_base = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": cfg["app_key"],
        "appsecret": cfg["app_secret"],
        "tr_id": TR_ID_PSEARCH,
        "custtype": "P",
    }

    items, tr_cont = [], ""
    meta = {"rt_cd": None, "msg_cd": "", "msg1": "", "http": None}

    for _ in range(MAX_PAGES):
        headers = dict(headers_base, tr_cont=tr_cont)
        res = requests.get(f"{cfg['base_url']}{PATH_PSEARCH}", headers=headers,
                           params={"user_id": cfg["user_id"], "seq": str(seq)},
                           timeout=10)

        meta["http"] = res.status_code
        if res.status_code != 200:
            return {"seq": seq, "items": [], "meta": meta, "error": f"HTTP {res.status_code}"}

        data = res.json()
        meta["rt_cd"] = data.get("rt_cd")
        meta["msg_cd"] = data.get("msg_cd", "")
        meta["msg1"] = data.get("msg1", "")

        for raw in (data.get("output2") or []):
            code = (raw.get("code") or raw.get("s_code") or "").strip()
            if not code:
                continue
            items.append({
                "code": code,
                "name": (raw.get("name") or "").strip(),
                "price": _to_float(raw.get("price")),
                "chgrate": _to_float(raw.get("chgrate")),
                "trade_amt": _to_float(raw.get("trade_amt")),
            })

        tr_cont = res.headers.get("tr_cont", "")
        if meta["rt_cd"] == "0" or not tr_cont:
            break

    items.sort(key=lambda x: -x["trade_amt"])
    return {"seq": seq, "items": items, "meta": meta, "error": None}


# ==============================
# 📊 요약 및 출력
# ==============================
def summarize(result, codes, use_market=True):
    """조건식 결과의 시장 구성/거래대금 요약"""
    items = result["items"]
    comp = {}
    if use_market:
        for item in items:
            comp[classify(item["code"], codes)] = comp.get(classify(item["code"], codes), 0) + 1

    summary = {
        "count": len(items),
        "comp": comp,
        "amount_sum": sum(i["trade_amt"] for i in items),
        "amount_max": items[0]["trade_amt"] if items else 0.0,
        "amount_min": items[-1]["trade_amt"] if items else 0.0,
        "top": items[:1],
        "last": items[-1:] if items else [],
    }

    if not items:
        summary["label"] = "결과 없음"
    elif not use_market:
        summary["label"] = "-"
    else:
        kosdaq = comp.get("KOSDAQ", 0)
        kospi = comp.get("KOSPI", 0)
        if kosdaq and not kospi:
            summary["label"] = "코스닥 전용으로 보임"
        elif kospi and not kosdaq:
            summary["label"] = "코스피 전용으로 보임"
        elif kosdaq and kospi:
            summary["label"] = f"혼합(코스닥 {kosdaq}/코스피 {kospi})"
        else:
            summary["label"] = "판별 불가(미상만)"

    return summary


def _status(meta, error, count):
    if error:
        return f"조회 실패 ({error})"
    if count == 0:
        if meta.get("msg_cd") == "MCA05762":
            return "저장된 조건식 없음"
        return f"0건 ({meta.get('msg_cd', '')})"
    return "정상"


def print_detail(result, codes, limit, use_market):
    seq = result["seq"]
    items = result["items"]
    summary = summarize(result, codes, use_market)
    meta = result["meta"]

    print(f"\n[seq={seq}]  HTS {seq + 1}번 조건식")
    print(f"   상태        : {_status(meta, result['error'], summary['count'])}"
          f"   (rt_cd={meta.get('rt_cd')}, {meta.get('msg_cd', '')} {meta.get('msg1', '')})")
    print(f"   조건식 이름 : API 미제공 (HTS 화면에서 확인해야 함)")
    print(f"   종목 수     : {summary['count']}종목")
    if items:
        print(f"   거래대금    : 합계 {summary['amount_sum'] / 1e8:,.1f}억 "
              f"| 최대 {summary['amount_max'] / 1e8:,.1f}억 "
              f"| 최소 {summary['amount_min'] / 1e8:,.1f}억")
    if use_market and items:
        comp = summary["comp"]
        print("   시장 구성   : " + (", ".join(f"{k} {v}" for k, v in comp.items()) or "-"))
        print(f"   추정 대상   : {summary['label']}")

    if not items:
        return

    print(f"   거래대금 상위 {min(limit, len(items))}종목:")
    for idx, item in enumerate(items[:limit], 1):
        tag = classify(item["code"], codes) if use_market else ""
        print(f"     {idx:>3}. {item['code']} {item['name']:<18}"
              f"{item['trade_amt'] / 1e8:>10,.1f}억  {tag}")

    if len(items) > limit:
        item = items[-1]
        tag = classify(item["code"], codes) if use_market else ""
        print(f"     ... 하위: {item['code']} {item['name']:<18}"
              f"{item['trade_amt'] / 1e8:>10,.1f}억  {tag}")


def print_table(rows):
    """전체 seq 종합 표"""
    widths = {"name": 10, "seq": 5, "status": 22, "count": 7,
              "target": 24, "amount": 14}
    line = "=" * 104

    print(f"\n{line}")
    print(" 종합")
    print(line)
    print(pad("조건식", widths["name"]) + pad("seq", widths["seq"])
          + pad("상태", widths["status"])
          + pad("종목수", widths["count"], "right") + "  "
          + pad("추정 대상", widths["target"])
          + pad("거래대금 합계", widths["amount"], "right") + "  상위 종목")
    print("-" * 104)

    for row in rows:
        top = "-"
        if row["summary"]["count"]:
            top = ", ".join(f"{i['name']}({i['trade_amt'] / 1e8:,.1f}억)"
                            for i in row["summary"]["top"][:2])
            if row["summary"]["count"] > 2:
                top += ", …"

        print(pad(f"{row['seq'] + 1}번", widths["name"])
              + pad(row["seq"], widths["seq"])
              + pad(row["status"], widths["status"])
              + pad(row["summary"]["count"], widths["count"], "right") + "  "
              + pad(row["summary"]["label"], widths["target"])
              + pad(f"{row['summary']['amount_sum'] / 1e8:,.1f}억",
                    widths["amount"], "right") + "  " + top)

    print("-" * 104)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="HTS 저장 조건검색식 번호/내용 확인 (조건식 이름은 API로 제공되지 않음)")
    parser.add_argument("--account", choices=list(ACCOUNTS), default="real",
                        help="조회 계정 (기본: real 실전)")
    parser.add_argument("--seq", type=int, action="append", default=None,
                        help="특정 seq 만 조회 (여러 번 지정 가능)")
    parser.add_argument("--start", type=int, default=0, help="순회 시작 seq (기본: 0)")
    parser.add_argument("--end", type=int, default=19, help="순회 종료 seq, 포함 (기본: 19)")
    parser.add_argument("--limit", type=int, default=5, help="조건식별 상위 종목 출력 개수")
    parser.add_argument("--no-market", action="store_true",
                        help="정적 목록(kospi.txt/kosdaq.txt)로 시장 분류하지 않음")
    parser.add_argument("--quiet", action="store_true", help="개별 조건식 상세가 아닌 종합 표만 출력")
    parser.add_argument("--json", default=None, help="결과를 JSON 파일로 저장")
    args = parser.parse_args(argv)

    cfg = dict(ACCOUNTS[args.account])
    cfg["app_key"] = os.getenv(cfg["app_key"])
    cfg["app_secret"] = os.getenv(cfg["app_secret"])
    cfg["user_id"] = os.getenv(cfg["user_id"])

    missing = [k for k in ("app_key", "app_secret", "user_id") if not cfg[k]]
    if missing:
        raise SystemExit(f"❌ {cfg['label']} 계정 환경변수가 없습니다: {', '.join(missing)}")
    cfg["user_id"] = cfg["user_id"].strip()

    codes = load_market_codes()
    seqs = sorted(set(args.seq)) if args.seq else list(range(args.start, args.end + 1))

    token = issue_token(cfg)

    print(f"■ 계정 : {cfg['label']} ({cfg['base_url']})")
    print(f"■ ID   : {cfg['user_id'][:3]}***")
    print(f"■ 조회 : seq {seqs[0]} ~ {seqs[-1]}  ({len(seqs)}건, 연속조회 포함)")
    print(f"■ 참고 : 조건검색 API는 조건식 '이름'을 반환하지 않는다. "
          f"시장 구성은 kospi.txt/kosdaq.txt 기준 판별이다.")

    rows, results = [], []
    for seq in seqs:
        result = fetch_condition(cfg, token, seq)
        summary = summarize(result, codes, use_market=not args.no_market)
        status = _status(result["meta"], result["error"], summary["count"])

        if not args.quiet:
            print_detail(result, codes, args.limit, not args.no_market)

        rows.append({"seq": seq, "status": status, "summary": summary})
        results.append({
            "seq": seq,
            "hts_seq": seq + 1,
            "status": status,
            "meta": result["meta"],
            "error": result["error"],
            "summary": {k: v for k, v in summary.items() if k != "top"},
            "items": result["items"],
        })

    print_table(rows)

    found = [r["seq"] for r in rows if r["summary"]["count"] > 0]
    if found:
        print(f"\n✅ 결과가 있는 조건식 seq: {found}  (HTS {[s + 1 for s in found]}번)")
    else:
        print("\n⚠️ 결과가 있는 조건식이 없습니다. HTS에 조건식이 저장되어 있는지, "
              "ID가 HTS 아이디와 일치하는지 확인하세요.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n💾 결과 저장: {args.json}")

    return results


if __name__ == "__main__":
    main()
