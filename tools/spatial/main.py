#!/usr/bin/env python
"""S3 L2 空间特征 CLI：价格面 / 高危区 / POI 密度 / 通勤近似 → ADS/DWS 落库。

用法:
    tools/orchestrator/.venv/bin/python tools/spatial/main.py --once
    tools/orchestrator/.venv/bin/python tools/spatial/main.py --once --date 2026-08-05
    tools/orchestrator/.venv/bin/python tools/spatial/main.py --once --dry-run   # 只算不写库

输出:
    - MySQL spacefin_crawler（root）：ads_spatial_zone(区块画像) / dws_spatial_feature(逐实体特征)
    - 文件：{out_dir}/spatial_report.json（行数/规则触发/缺失率统计）

口径摘要（详见 docs/tech/components/spatial-feature.md）：
    - 价格面：DWD 有坐标挂牌行建 cKDTree，半径 5km 邻域中位单价为「局部价格基准」；
      每行/每小区价格偏差 = (单价-邻域中位)/邻域中位。
    - 高危区 A（价格洼地）：区块中位单价 <= 城市中位 * (1-0.25) 且样本 >= 20。
    - 高危区 B（LTV 集中）：抵押物网格内 LTV 中位 > 红线且样本 >= 3。
    - POI 密度：半径 2km 内挂牌数/面积（挂牌密度代理，非真实 POI）。
    - 通勤：到最近城市中心直线距离 / 30km/h（无路网直线近似）。
    - 单机 cKDTree 实现（Sedona 的 MVP 降级，见规划 R-tech-1）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import features
import store

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    ap = argparse.ArgumentParser(description="S3 L2 空间特征计算与落库")
    ap.add_argument("--date", default=config.business_date(), help="构建日期 YYYY-MM-DD")
    ap.add_argument("--out-dir", default="output/spatial", help="报告输出目录")
    ap.add_argument("--once", action="store_true", help="跑完即退出（批处理口径，默认行为）")
    ap.add_argument("--dry-run", action="store_true", help="只计算并打印统计，不写库")
    args = ap.parse_args()

    env = config.load_env()
    # 读 DWD 走 app 账号（只读）；读业务库与建表/写库走 root
    crawl = store.connect(config.crawl_params(env))
    biz = store.connect(config.business_params(env))
    t0 = time.time()

    sale_rows = store.load_sale_rows(crawl)
    collaterals = store.load_collaterals(biz)
    loans = store.load_loans(biz)
    features.enrich_ltv(collaterals, loans)
    print(
        f"[spatial] sale_with_coord={len(sale_rows)} collaterals={len(collaterals)} "
        f"loans={len(loans)}"
    )

    # --- 价格面 + 高危区 ---
    tree, coords, prices = features.build_price_surface(sale_rows)
    zones, zone_index = features.build_price_zones(sale_rows)
    ltv_zones, ltv_zone_index = features.build_ltv_zones(collaterals)

    # --- DWS 特征 ---
    listing_feats = features.build_listing_features(sale_rows, tree, coords, prices, zone_index)
    comm_feats = features.build_community_features(sale_rows, tree, coords, prices, zone_index)
    collat_feats = features.build_collateral_features(
        collaterals, tree, coords, prices, ltv_zone_index
    )

    # --- 统计 ---
    high_risk_price = [z for z in zones if z["is_high_risk_zone"]]
    high_risk_ltv = [z for z in ltv_zones if z["is_high_risk_zone"]]
    missing_ge75 = [f for f in collat_feats if f["spatial_feat_missing_pct"] >= 75]
    report = {
        "build_date": args.date,
        "seed": config.SEED,
        "n_sale_with_coord": len(sale_rows),
        "n_listing_features": len(listing_feats),
        "n_community_features": len(comm_feats),
        "n_collateral_features": len(collat_feats),
        "n_price_zones": len(zones),
        "n_high_risk_price_zones": len(high_risk_price),
        "n_ltv_zones": len(ltv_zones),
        "n_high_risk_ltv_zones": len(high_risk_ltv),
        "collateral_missing_ge75": len(missing_ge75),
        "high_risk_rule_a": f"zone_median_price <= city_median*(1-{config.PRICE_LOW_RATIO}) "
        f"and samples>={config.MIN_ZONE_SAMPLES}",
        "high_risk_rule_b": f"zone_median_ltv > {config.LTV_RED_LINE} "
        f"and samples>={config.MIN_LTV_ZONE_SAMPLES}",
    }

    os.makedirs(args.out_dir, exist_ok=True)
    report_path = os.path.join(args.out_dir, "spatial_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"[spatial] 计算完成 {time.time() - t0:.1f}s | report={report_path}")

    if args.dry_run:
        print("[spatial] --dry-run：跳过写库")
    else:
        wconn = store.connect(config.root_crawl_params(env))
        store.ensure_tables(wconn)
        nz = store.upsert_zones(wconn, zones + ltv_zones, args.date)
        nf = store.upsert_features(wconn, listing_feats + comm_feats + collat_feats, args.date)
        wconn.close()
        print(f"[spatial] ADS/DWS 已写: ads_spatial_zone={nz} dws_spatial_feature={nf}")

    crawl.close()
    biz.close()


if __name__ == "__main__":
    main()
