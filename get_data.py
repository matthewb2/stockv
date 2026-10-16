from datetime import datetime, timedelta
import warnings
import pandas as pd
import yfinance as yf

# 판다스 concat 관련 FutureWarning 숨기기
warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    message=".*The behavior of DataFrame concatenation with empty or all-NA entries is deprecated.*",
)

def update2_csv_data(
    input_path="",
    output_path="",
    ticker="^DJI",
    index_col=5,
    return_col=None,
):
    """CSV 파일의 날짜와 yfinance 데이터를 한 줄씩 대조하여,

    객체에 없는 날짜(CSV 고유 날짜)는 보존하고 존재하는 날짜에만 지수와 수익률을 정확히 기록하는 함수.
    """
    # 1. 기존 CSV 파일 읽기
    try:
        df = pd.read_csv(input_path, header=0)
    except FileNotFoundError:
        df = pd.DataFrame()

    if df.empty or df.shape[1] == 0:
        print(f"[{ticker}] 파일이 비어있거나 읽을 수 없습니다.")
        return df

    date_col = df.columns[0]

    # 2. 기존 날짜 범위 파악 및 정제 (YY.MM.DD 형식 기준)
    df = df.dropna(subset=[date_col]).reset_index(drop=True)
    df[date_col] = df[date_col].astype(str).str.strip()

    valid_mask = df[date_col].str.contains(r"\d{2}\.\d{2}\.\d{2}", regex=True)
    valid_dates = df.loc[valid_mask, date_col]

    if not valid_dates.empty:
        min_date_val = valid_dates.min()
        min_date_str = "20" + min_date_val.replace(".", "-")
        start_date_dt = datetime.strptime(min_date_str, "%Y-%m-%d") - timedelta(days=15)
    else:
        start_date_dt = datetime.today() - timedelta(days=365)

    end_date_dt = datetime.today() + timedelta(days=1)

    # 3. yfinance를 통한 데이터 다운로드
    asset = yf.Ticker(ticker)
    hist = asset.history(
        start=start_date_dt.strftime("%Y-%m-%d"),
        end=end_date_dt.strftime("%Y-%m-%d"),
    )

    if hist.empty:
        print(f"[{ticker}] 다운로드된 데이터가 없습니다.")
        return df

    # 타임존 및 시간 제거 후 'YY.MM.DD' 문자열로 변환하고 딕셔너리로 매핑 준비
    hist.index = pd.to_datetime(hist.index).tz_localize(None).strftime("%y.%m.%d")
    mapping_dict = hist["Close"].to_dict()

    # 4. CSV에 아직 존재하지 않는 최신 날짜(yfinance에만 있는 날짜)가 있다면 상단에 추가 (Append)
    existing_dates = set(df[date_col])
    new_dates = [d for d in hist.index if d not in existing_dates]

    if new_dates:
        new_rows = []
        for d in new_dates:
            row = {col: None for col in df.columns}
            row[date_col] = d
            new_rows.append(row)

        new_df = pd.DataFrame(new_rows, columns=df.columns)
        df = pd.concat([new_df, df], ignore_index=True)

    # 5. 날짜 기준 오름차순(과거 -> 최신) 정렬
    df = df.sort_values(by=date_col, ascending=True).reset_index(drop=True)

    target_idx_col = df.columns[index_col]
    target_ret_col = df.columns[return_col] if return_col is not None else None

    # 6. 레코드를 한 줄씩 순회하며 날짜 대조 및 지수 기록
    # (CSV에는 객체에 없는 날짜가 포함되어 있으므로, mapping_dict에 존재하는 날짜에만 값을 채웁니다.)
    for i in range(len(df)):
        current_date = df.loc[i, date_col]
        
        if current_date in mapping_dict:
            df.loc[i, target_idx_col] = round(mapping_dict[current_date], 2)

    df[target_idx_col] = pd.to_numeric(df[target_idx_col], errors="coerce")

    # 7. 수익률 계산 (중간에 yfinance 데이터가 없는 날짜가 섞여있어도 직전 유효 거래일과 비교하여 계산)
    if target_ret_col is not None:
        for i in range(len(df)):
            curr_val = df.loc[i, target_idx_col]
            
            if pd.notna(curr_val):
                # 직전 유효한 지수 값을 역방향으로 탐색하여 가져옴
                prev_val = None
                for p in range(i - 1, -1, -1):
                    val = df.loc[p, target_idx_col]
                    if pd.notna(val):
                        prev_val = val
                        break
                
                if prev_val is not None and prev_val != 0:
                    ret = ((curr_val - prev_val) / prev_val) * 100
                    df.loc[i, target_ret_col] = round(ret, 2)
            else:
                # yfinance 데이터가 없어 지수가 없는 날은 수익률도 비워둠
                df.loc[i, target_ret_col] = pd.NA

    # 8. 최종 저장을 위해 다시 '내림차순(최신 -> 과거)'으로 정렬
    df = df.sort_values(by=date_col, ascending=False).reset_index(drop=True)

    if target_ret_col is not None:
        print_cols = [date_col, target_idx_col, target_ret_col]
    else:
        print_cols = [date_col, target_idx_col]

    # 9. 결과 저장 (헤더 포함)
    df.to_csv(output_path, index=False, header=True, encoding="utf-8-sig")

    print(f"=== [{ticker}] update2 완료 ===")
    print(df[print_cols].head())
    return df
    
if __name__ == "__main__":
    # 다우지수 업데이트 예시 호출
    update2_csv_data(
        input_path="market_data.csv",
        output_path="market_data.csv",
        ticker="^DJI",
        index_col=5,
        return_col=6,
    )
        # 다우지수 업데이트 예시 호출
    update2_csv_data(
        input_path="kosdaq_data.csv",
        output_path="kosdaq_data.csv",
        ticker="^IXIC",
        index_col=5,
        return_col=6,
    )
    update2_csv_data(
        input_path="kosdaq_data.csv",
        output_path="kosdaq_data.csv",
        ticker="^KQ11",
        index_col=8,
        return_col=9,
    )

    # 4. 방금 다우가 업데이트된 'market_data.csv'를 다시 읽어 코스피(^KS11)를 이어서 업데이트
    update2_csv_data(
        input_path="market_data.csv",
        output_path="market_data.csv",
        ticker="^KS11",
        index_col=8,
        return_col=9,
    )
    
    update2_csv_data(
        input_path="kosdaq_data.csv",
        output_path="kosdaq_data.csv",
        ticker="USDKRW=X",
        index_col=11        
    )
    
    update2_csv_data(
        input_path="market_data.csv",
        output_path="market_data.csv",
        ticker="USDKRW=X",
        index_col=11
    )