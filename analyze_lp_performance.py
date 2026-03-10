import argparse
from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class MetricsConfig:
    """
    配置说明：
    - trades_csv: 交易明细，每一行是一笔成交或部分成交。
      建议（非强制）列名：
        - timestamp: 成交时间（可被 pandas 解析）
        - asset: token / asset_id
        - side: BUY / SELL
        - price: 成交价（小数，如 0.45）
        - size: 成交数量（shares）
        - fee: 手续费（USDC，maker 为负、rebate 为正）
        - reward: 流动性奖励（USDC，可选；没有则视为 0）
    """

    trades_csv: str
    time_col: str = "timestamp"
    asset_col: str = "asset"
    side_col: str = "side"
    price_col: str = "price"
    size_col: str = "size"
    fee_col: str = "fee"
    reward_col: str = "reward"


def load_trades(cfg: MetricsConfig) -> pd.DataFrame:
    df = pd.read_csv(cfg.trades_csv)
    # 处理时间列
    if cfg.time_col in df.columns:
        df[cfg.time_col] = pd.to_datetime(df[cfg.time_col], errors="coerce")
    else:
        df[cfg.time_col] = pd.NaT

    # 必要数值列
    for col in (cfg.price_col, cfg.size_col, cfg.fee_col):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        else:
            df[col] = 0.0

    # 奖励列可选
    if cfg.reward_col in df.columns:
        df[cfg.reward_col] = pd.to_numeric(df[cfg.reward_col], errors="coerce").fillna(0.0)
    else:
        df[cfg.reward_col] = 0.0

    # 方向：BUY 为 +1，SELL 为 -1
    if cfg.side_col in df.columns:
        df["_dir"] = df[cfg.side_col].astype(str).str.upper().map({"BUY": 1, "SELL": -1}).fillna(0.0)
    else:
        df["_dir"] = 0.0

    return df


def compute_metrics(df: pd.DataFrame, cfg: MetricsConfig) -> pd.DataFrame:
    """
    以 asset + 日期 为粒度计算：
    - 成交名义金额、净持仓变化
    - 手续费、奖励
    - 近似点差 / 方向性 PnL（基于买卖价差的粗略估算）
    """
    if df.empty:
        return pd.DataFrame()

    df = df.copy()
    if df[cfg.time_col].isna().all():
        df["date"] = "unknown"
    else:
        df["date"] = df[cfg.time_col].dt.date.astype(str)

    # 方向签名的名义成交额（以 USDC 计）
    df["signed_notional"] = df["_dir"] * df[cfg.price_col] * df[cfg.size_col]

    # 简单假设：同一 asset + 日期 内，signed_notional 的和大致反映方向性 PnL
    # （正值表示整体偏多头、负值偏空头；此处只做粗略参考，真实 PnL 仍以仓位 + 最终平仓价为准）

    grp_keys = [cfg.asset_col, "date"]

    grouped = df.groupby(grp_keys, dropna=False).agg(
        trades_count=("signed_notional", "size"),
        volume_shares=(cfg.size_col, "sum"),
        gross_notional=("signed_notional", "sum"),
        fee_total=(cfg.fee_col, "sum"),
        reward_total=(cfg.reward_col, "sum"),
    ).reset_index()

    grouped["net_reward_minus_fee"] = grouped["reward_total"] - grouped["fee_total"]

    # 对于流动性做市，通常关注「单位风险/单位成交量的奖励」
    grouped["reward_per_1k_notional"] = grouped["reward_total"] / (grouped["gross_notional"].abs() / 1000.0).replace(0, pd.NA)
    grouped["net_pnl_proxy"] = grouped["net_reward_minus_fee"]  # 目前仅统计奖励-手续费部分

    return grouped


def summarize_overall(metrics_df: pd.DataFrame) -> None:
    if metrics_df.empty:
        print("⚠️ 没有任何可用交易记录，无法计算指标。")
        return

    total_trades = int(metrics_df["trades_count"].sum())
    total_volume = float(metrics_df["volume_shares"].sum())
    total_notional = float(metrics_df["gross_notional"].abs().sum())
    total_fee = float(metrics_df["fee_total"].sum())
    total_reward = float(metrics_df["reward_total"].sum())
    net_rf = float(metrics_df["net_reward_minus_fee"].sum())

    print("\n================= LP 策略总体汇总 =================")
    print(f"成交笔数           : {total_trades}")
    print(f"成交总量 (shares)  : {total_volume:,.0f}")
    print(f"名义成交额 (USDC)  : {total_notional:,.2f}")
    print(f"总手续费 (USDC)    : {total_fee:,.4f}")
    print(f"总奖励   (USDC)    : {total_reward:,.4f}")
    print(f"奖励-手续费 (USDC) : {net_rf:,.4f}")
    if total_notional > 0:
        print(f"奖励-手续费 / 名义成交额 : {net_rf / total_notional * 10000:,.2f} bp")
    print("=================================================\n")

    # 按 asset 维度排行，找出「最赚钱/最亏钱」资产
    by_asset = (
        metrics_df.groupby("asset", dropna=False)[["net_reward_minus_fee", "reward_total", "fee_total"]]
        .sum()
        .sort_values("net_reward_minus_fee", ascending=False)
    )
    print("📊 按资产汇总（前 20 条）：")
    print(by_asset.head(20).to_string(float_format=lambda x: f"{x:,.4f}"))
    print()


def summarize_backtest_compare(baseline_csv: str, optimized_csv: str) -> None:
    base = pd.read_csv(baseline_csv)
    opt = pd.read_csv(optimized_csv)
    required = ["net_pnl", "reward", "fees", "max_drawdown", "max_single_close_loss"]
    for col in required:
        if col not in base.columns:
            base[col] = 0.0
        if col not in opt.columns:
            opt[col] = 0.0

    base_sum = base[required].sum(numeric_only=True)
    opt_sum = opt[required].sum(numeric_only=True)
    diff = opt_sum - base_sum

    print("\n================= 参数对比（旧 vs 新） =================")
    print(f"基线净收益 net_pnl          : {base_sum['net_pnl']:,.4f}")
    print(f"优化净收益 net_pnl          : {opt_sum['net_pnl']:,.4f}")
    print(f"净收益变化                  : {diff['net_pnl']:,.4f}")
    print(f"奖励变化 reward             : {diff['reward']:,.4f}")
    print(f"费用变化 fees               : {diff['fees']:,.4f}")
    print(f"回撤变化 max_drawdown       : {diff['max_drawdown']:,.4f} (越接近0越好)")
    print(f"单次清仓损失变化            : {diff['max_single_close_loss']:,.4f} (越接近0越好)")
    print("========================================================\n")


def summarize_bucket_best(candidates_csv: str) -> None:
    cand = pd.read_csv(candidates_csv)
    if cand.empty or "bucket_name" not in cand.columns or "score" not in cand.columns:
        print("⚠️ 候选参数文件缺少 bucket_name/score，跳过分桶报告。")
        return
    cand = cand.sort_values(["bucket_name", "score"], ascending=[True, False])
    best = cand.groupby("bucket_name", as_index=False).head(1)
    cols = [c for c in ["bucket_name", "score", "risk_ok", "net_pnl", "max_drawdown", "max_single_close_loss"] if c in best.columns]
    print("📌 分时段最优参数表现：")
    print(best[cols].to_string(index=False, float_format=lambda x: f"{x:,.4f}"))
    print()


def main():
    parser = argparse.ArgumentParser(description="从历史交易/订单数据中统计 LP 策略的奖励、手续费与简单 PnL 代理。")
    parser.add_argument("--trades-csv", required=False, default=None, help="包含历史成交记录的 CSV 文件路径")
    parser.add_argument("--out-csv", default=None, help="导出按 asset+date 维度汇总后的指标到该 CSV")
    parser.add_argument("--baseline-backtest-csv", default=None, help="旧参数回测输出 CSV（含 net_pnl 等字段）")
    parser.add_argument("--optimized-backtest-csv", default=None, help="新参数回测输出 CSV（含 net_pnl 等字段）")
    parser.add_argument("--candidates-csv", default=None, help="optimize_params 导出的候选参数 CSV，用于分时段最优报告")
    args = parser.parse_args()

    if args.trades_csv:
        cfg = MetricsConfig(trades_csv=args.trades_csv)
        trades = load_trades(cfg)
        metrics_df = compute_metrics(trades, cfg)

        summarize_overall(metrics_df)

        if args.out_csv:
            metrics_df.to_csv(args.out_csv, index=False)
            print(f"✅ 已导出按 asset+date 汇总结果到: {args.out_csv}")

    if args.baseline_backtest_csv and args.optimized_backtest_csv:
        summarize_backtest_compare(args.baseline_backtest_csv, args.optimized_backtest_csv)

    if args.candidates_csv:
        summarize_bucket_best(args.candidates_csv)


if __name__ == "__main__":
    main()

