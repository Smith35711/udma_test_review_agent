#!/usr/bin/env python3
"""检视质量离线评测：对既有产物算 recall / precision / 矛盾率 / 重复率（零额度）。

用法:
  # 评测单次运行
  python scripts/eval_review.py --run output/review
  # 前后对比（基线 vs 改造后）
  python scripts/eval_review.py --run output/review --baseline /path/to/baseline/review

产物: <run>/eval_report.md（人类可读）与 stdout 的 JSON 摘要（机器可读）。

为什么这样定义指标
------------------
朴素的「confirmed / 全部 findings = 精确率」在方向上是错的：按反驳提示词的契约，
`refuted` 必须给出具体反证，所以**一条被正确反驳的 finding 是反驳阶段的成功，不是误报**。
按那个定义打分，一个把所有东西都压下去的实现会拿到最高分。因此本脚本分开报告：

- `precision_confirmed` —— 在**已确认**条目里，有多少对得上人工 ground truth（真正的误报率）；
- `suppression_correctness` —— 在**已反驳**条目里，有多少确实不在 ground truth 中（正确压制）；
- `false_refuted` —— 被反驳但**确实存在**的缺陷，即漏报，是这条流水线最危险的失败模式；
- `recall_high` —— 高严重度内核的召回，作为主指标。

`contradiction_rate` 按**同一主张**（锚点 + 缺陷种类）统计，不按同一行统计：同一语句上
的不同主张得出不同结论是正常的（实测 `parser.c:13` 上三个都成立的缺陷分别是
confirmed / refuted / refuted），把那种情况算作矛盾会掩盖真正的矛盾。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402

from udma_test_review_agent import findings_util as fu  # noqa: E402

HIGH_SEVERITIES = ("致命", "严重")


# ---------------------------------------------------------------- 载入


def load_ground_truth(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key in ("defects", "non_defects"):
        data.setdefault(key, [])
    return data


def load_run(run_dir: Path) -> list[dict]:
    """读取一次运行的全部模块产物，展平为 `[{module, finding, verdict}]`。"""
    items: list[dict] = []
    for module_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        verified = module_dir / "findings_verified.json"
        if not verified.exists():
            continue
        payload = json.loads(verified.read_text(encoding="utf-8"))
        for result in payload.get("results", []):
            items.append({"module": module_dir.name, "finding": result["finding"], "verdict": result["verdict"]})
    return items


def load_observations(run_dir: Path) -> list[dict]:
    observations: list[dict] = []
    for module_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        verified = module_dir / "findings_verified.json"
        if not verified.exists():
            continue
        payload = json.loads(verified.read_text(encoding="utf-8"))
        for obs in payload.get("observations", []):
            observations.append({"module": module_dir.name, "finding": obs})
    return observations


# ---------------------------------------------------------------- 匹配


def _at_anchor(finding: dict, defect: dict, tolerance: int) -> bool:
    if str(finding.get("file") or "") != str(defect.get("file") or ""):
        return False
    return abs(int(finding.get("line") or 0) - int(defect.get("line") or 0)) <= tolerance


def anchor_buckets(defects: list[dict], tolerance: int) -> list[list[dict]]:
    """把 ground truth 按 (file, 行号±容差) 归并；同一锚点上的多条缺陷需被多条 finding 覆盖。"""
    buckets: list[list[dict]] = []
    for defect in defects:
        for bucket in buckets:
            if _at_anchor(defect, bucket[0], tolerance):
                bucket.append(defect)
                break
        else:
            buckets.append([defect])
    return buckets


def _assign_credit(bucket: list[dict], findings: list[dict]) -> set[str]:
    """把锚点内的已确认条目分配给该锚点上的各条 ground truth，返回被命中的 GT id。

    **不能按位置分配**：实测 `resource.c:7` 上同时有 E13（fopen 未检查，memory）、
    E14（fd 泄漏，resource）、E18（path 未校验，trust_boundary）三条，
    而该锚点唯一被确认的是 E14，位置分配会把功劳记给排在前面的 E13——
    于是 E14 被记为漏报、E13 被记为召回，两个方向都错。

    分配优先级：族一致 > 标题词重叠 > 严重度更高 > YAML 顺序。
    族只用于**同一锚点内**的区分，不参与跨锚点匹配（跨运行 category 漂移实测严重，
    但「同一行上这三条里是哪一条」这个判断要弱得多，族足以胜任）。
    """
    remaining = list(bucket)
    hit: set[str] = set()
    for finding in findings:
        if not remaining:
            break
        fam = fu.category_family(finding.get("category"))
        text = " ".join(
            str(finding.get(k) or "") for k in ("title", "description", "root_cause", "impact", "suggestion")
        )

        def rank(defect: dict) -> tuple:
            overlap = sum(1 for kw in _keywords(defect) if kw in text)
            return (
                0 if defect.get("family") == fam else 1,
                -overlap,
                fu.severity_rank(defect.get("severity")),
            )

        best = min(remaining, key=rank)
        remaining.remove(best)
        hit.add(best["id"])
    return hit


def _keywords(defect: dict) -> list[str]:
    """从 ground truth 的 title/why 中提取用于比对的特征词（含中英）。"""
    text = f"{defect.get('title', '')} {defect.get('why', '')}"
    tokens = {t for t in text.replace("，", " ").replace("、", " ").replace("（", " ").replace("）", " ").split() if len(t) >= 3}
    tokens |= {k for k in ("fclose", "fopen", "malloc", "free", "strcpy", "memcpy", "fread", "NUL", "NULL") if k in text}
    return sorted(tokens)


def score(gt: dict, items: list[dict], observations: list[dict]) -> dict:
    tolerance = int(gt.get("line_tolerance") or 1)
    misanchor_window = int(gt.get("misanchor_window") or 3)
    confirmed = [i for i in items if i["verdict"]["conclusion"] == "confirmed"]
    refuted = [i for i in items if i["verdict"]["conclusion"] == "refuted"]
    uncertain = [i for i in items if i["verdict"]["conclusion"] == "uncertain"]

    # ---- recall：按锚点分组，锚点内按特征把已确认条目分配给各条 ground truth
    hit_ids: set[str] = set()
    missed: list[str] = []
    for bucket in anchor_buckets(gt["defects"], tolerance):
        covering = [i["finding"] for i in confirmed if any(_at_anchor(i["finding"], d, tolerance) for d in bucket)]
        hit_ids |= _assign_credit(bucket, covering)
        missed.extend(d["id"] for d in bucket if d["id"] not in hit_ids)

    high = [d for d in gt["defects"] if d.get("severity") in HIGH_SEVERITIES]
    high_hit = [d for d in high if d["id"] in hit_ids]

    # ---- precision：已确认条目中，有多少落在 ground truth 上
    def on_gt(item: dict, window: int) -> bool:
        return any(_at_anchor(item["finding"], d, window) for d in gt["defects"])

    tp = [i for i in confirmed if on_gt(i, tolerance)]
    # 锚点偏移的**真阳性**：本行坐标对不上，但放宽窗口内有 ground truth。
    # 单独统计，避免把「缺陷真实存在但定位到了函数签名行」误记成假阳性。
    misanchored = [i for i in confirmed if i not in tp and on_gt(i, misanchor_window)]
    false_positives = [i for i in confirmed if i not in tp and i not in misanchored]

    # ---- 显式误报：命中了「明确不是缺陷」清单
    on_non_defects = [
        i for i in confirmed if any(_at_anchor(i["finding"], d, tolerance) for d in gt["non_defects"])
    ]

    # ---- 压制正确性
    # 「漏报」的定义必须收紧：只有**未被任何已确认条目覆盖**的 ground truth 被反驳，才算误驳。
    # 否则同一锚点上的冗余主张（如 buffer.c:7 上既 confirmed 了无界写入、又把信任面措辞反驳掉）
    # 会被记成「反驳了真缺陷」，把噪声当漏报。
    miss_set = set(missed)
    covering_confirmed_ids = {
        d["id"]
        for bucket in anchor_buckets(gt["defects"], tolerance)
        for d in bucket
        if any(_at_anchor(i["finding"], d, tolerance) for i in confirmed)
    }
    refuted_on_uncovered: list[dict] = []
    for i in refuted:
        for d in gt["defects"]:
            if d["id"] in miss_set and d["id"] not in covering_confirmed_ids and _at_anchor(i["finding"], d, tolerance):
                refuted_on_uncovered.append({**i, "gt_id": d["id"]})
                break
    correct_suppression = [i for i in refuted if i not in refuted_on_uncovered
                           and not any(_at_anchor(i["finding"], d, tolerance) for d in gt["defects"])]

    # ---- 矛盾率：同一主张（锚点 + 缺陷种类）内结论不一致
    by_claim: dict[tuple, set[str]] = defaultdict(set)
    for item in items:
        by_claim[fu.claim_key(item["finding"])].add(item["verdict"]["conclusion"])
    contested = {k: v for k, v in by_claim.items() if len(v) > 1}
    contradictory = {k: v for k, v in contested.items() if {"confirmed", "refuted"} <= v}

    # ---- 重复率：findings + observations 里的冗余主张
    all_findings = [i["finding"] for i in items] + [o["finding"] for o in observations]
    keys = [fu.claim_key(f) for f in all_findings]
    duplicates = len(keys) - len(set(keys))

    # ---- id 冲突：**按模块**统计。id 是模块内序号（每个模块都有 f1..f7），
    # 跨模块同名是设计如此，全局统计会把正常现象报成缺陷。
    id_collisions = 0
    for module in {i["module"] for i in items} | {o["module"] for o in observations}:
        ids = [i["finding"].get("id") for i in items if i["module"] == module]
        ids += [o["finding"].get("id") for o in observations if o["module"] == module]
        id_collisions += sum(c - 1 for c in Counter(ids).values() if c > 1)

    total_confirmed = len(confirmed) or 1
    total_refuted = len(refuted) or 1
    total_findings = len(items) or 1

    return {
        "run": {
            "findings": len(items),
            "observations": len(observations),
            "confirmed": len(confirmed),
            "refuted": len(refuted),
            "uncertain": len(uncertain),
            "high_severity_confirmed": sum(
                1 for i in confirmed if fu.norm_severity(i["verdict"].get("severity")) in HIGH_SEVERITIES
            ),
        },
        "recall": {
            "high_gt_total": len(high),
            "high_gt_hit": len(high_hit),
            "high": round(len(high_hit) / (len(high) or 1), 4),
            "all_gt_total": len(gt["defects"]),
            "all_gt_hit": len(hit_ids),
            "all": round(len(hit_ids) / (len(gt["defects"]) or 1), 4),
            "missed_ids": sorted(missed),
        },
        "precision": {
            "confirmed_on_gt": len(tp),
            "confirmed_misanchored": len(misanchored),
            "confirmed_off_gt": len(false_positives),
            "confirmed": round(len(tp) / total_confirmed, 4),
            "on_non_defects": len(on_non_defects),
            "misanchored_ids": sorted(
                f"{i['module']}/{i['finding'].get('id')}@{i['finding'].get('file')}:{i['finding'].get('line')}"
                for i in misanchored
            ),
            "off_gt_ids": sorted(i["module"] + "/" + str(i["finding"].get("id")) for i in false_positives),
        },
        "suppression": {
            "correct": len(correct_suppression),
            "false_refuted": len(refuted_on_uncovered),
            "neutral": len(refuted) - len(correct_suppression) - len(refuted_on_uncovered),
            "false_refutation_rate": round(len(refuted_on_uncovered) / total_refuted, 4),
            "false_refuted_ids": sorted(
                f"{i['gt_id']} 被 {i['module']}/{i['finding'].get('id')}@{i['finding'].get('file')}:{i['finding'].get('line')} 驳回"
                for i in refuted_on_uncovered
            ),
        },
        "contradiction": {
            "claims": len(by_claim),
            "contested": len(contested),
            "contradictory": len(contradictory),
            "rate": round(len(contradictory) / (len(by_claim) or 1), 4),
        },
        "duplication": {
            "total_claims": len(keys),
            "duplicate_claims": duplicates,
            "rate": round(duplicates / (len(keys) or 1), 4),
            "observation_share": round(len(observations) / (total_findings + len(observations) or 1), 4),
            "id_collisions": id_collisions,
        },
    }


# ---------------------------------------------------------------- 报告

_ROWS = [
    ("高严重度召回 (主指标)", lambda s: f"{s['recall']['high_gt_hit']}/{s['recall']['high_gt_total']}", lambda s: s["recall"]["high"]),
    ("全量召回", lambda s: f"{s['recall']['all_gt_hit']}/{s['recall']['all_gt_total']}", lambda s: s["recall"]["all"]),
    ("已确认精确率", lambda s: f"{s['precision']['confirmed_on_gt']}/{s['run']['confirmed']}", lambda s: s["precision"]["confirmed"]),
    ("锚点偏移的真阳性", lambda s: f"{s['precision']['confirmed_misanchored']}", lambda s: -s["precision"]["confirmed_misanchored"]),
    ("真缺陷被驳回 (越低越好)", lambda s: f"{s['suppression']['false_refuted']}/{s['run']['refuted']}", lambda s: -s["suppression"]["false_refuted"]),
    ("矛盾率 (越低越好)", lambda s: f"{s['contradiction']['contradictory']}/{s['contradiction']['claims']}", lambda s: s["contradiction"]["rate"]),
    ("重复率 (越低越好)", lambda s: f"{s['duplication']['duplicate_claims']}/{s['duplication']['total_claims']}", lambda s: s["duplication"]["rate"]),
    ("observations 占比", lambda s: f"{s['run']['observations']}", lambda s: s["duplication"]["observation_share"]),
    ("误配「非缺陷」清单 (越低越好)", lambda s: f"{s['precision']['on_non_defects']}", lambda s: -s["precision"]["on_non_defects"]),
    ("模块内 id 冲突 (越低越好)", lambda s: f"{s['duplication']['id_collisions']}", lambda s: -s["duplication"]["id_collisions"]),
]


def _delta_rows(stats: dict, baseline: dict | None) -> list[str]:
    if not baseline:
        return ["| 指标 | 值 |", "|---|---|"] + [f"| {n} | {show(stats)} |" for n, show, _ in _ROWS]
    rows = ["| 指标 | 本次 | 基线 | 变化 |", "|---|---|---|---|"]
    for name, show, key in _ROWS:
        cur, base = key(stats), key(baseline)
        delta = cur - base
        arrow = "→" if abs(delta) < 1e-9 else ("↑" if delta > 0 else "↓")
        rows.append(f"| {name} | {show(stats)} | {show(baseline)} | {arrow} {delta:+.4f} |")
    return rows


def render_report(stats: dict, baseline: dict | None, run_label: str, base_label: str) -> str:
    lines = ["# 检视质量评测报告", "", f"运行产物: `{run_label}`", ""]
    lines += ["## 指标", "", *_delta_rows(stats, baseline), ""]

    gt_by_id = {d["id"]: d for d in stats["_gt"]["defects"]}
    miss = stats["recall"]["missed_ids"]
    high_miss = [m for m in miss if gt_by_id.get(m, {}).get("severity") in HIGH_SEVERITIES]
    if high_miss:
        lines += ["## 未召回的高严重度内核", ""]
        for mid in high_miss:
            d = gt_by_id[mid]
            lines.append(f"- **{mid}** `{d['file']}:{d['line']}` [{d['severity']}] {d['title']}")
        lines += [""]
    if miss:
        lines += [f"其余未召回（一般/提示）: {', '.join(m for m in miss if m not in high_miss) or '无'}", ""]
    if stats["suppression"]["false_refuted_ids"]:
        lines += ["## 真缺陷被驳回（漏报，最危险）", ""]
        lines += [f"- {x}" for x in stats["suppression"]["false_refuted_ids"]]
        lines += [""]
    if stats["precision"]["misanchored_ids"]:
        lines += ["## 锚点偏移的真阳性（缺陷真实存在，但定位到邻近行）", ""]
        lines += [f"- `{x}`" for x in stats["precision"]["misanchored_ids"]]
        lines += [""]
    if stats["precision"]["off_gt_ids"]:
        lines += ["## 已确认但不在 ground truth 中（误报或清单外）", ""]
        lines += [f"- `{x}`" for x in stats["precision"]["off_gt_ids"]]
        lines += [""]
    lines += [
        "## 指标口径",
        "",
        "- 召回按 `(file, 行号±容差)` 匹配，**不含 category**（实测 category 跨运行漂移）。",
        "- 精确率只统计 `confirmed`；`refuted` 计入「压制正确率」——按反驳契约，正确反驳是成功而非误报。",
        "- 矛盾率按同一主张（锚点 + 缺陷种类）统计：同一语句上的不同主张结论不同属正常。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="检视质量离线评测")
    parser.add_argument("--run", required=True, help="本次运行的产物目录（含 M01/ 等模块子目录）")
    parser.add_argument("--baseline", default="", help="对比基线产物目录")
    parser.add_argument("--ground-truth", default="", help="ground truth YAML，默认 eval/expected_findings.yaml")
    parser.add_argument("--json", action="store_true", help="只打印 JSON 摘要")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    gt_path = Path(args.ground_truth) if args.ground_truth else repo_root / "eval" / "expected_findings.yaml"
    gt = load_ground_truth(gt_path)

    run_dir = Path(args.run)
    if not run_dir.is_dir():
        raise SystemExit(f"运行产物目录不存在: {run_dir}")

    stats = score(gt, load_run(run_dir), load_observations(run_dir))
    stats["_gt"] = gt

    baseline_stats = None
    if args.baseline:
        base_dir = Path(args.baseline)
        if not base_dir.is_dir():
            raise SystemExit(f"基线目录不存在: {base_dir}")
        baseline_stats = score(gt, load_run(base_dir), load_observations(base_dir))
        baseline_stats["_gt"] = gt

    report = render_report(stats, baseline_stats, args.run, args.baseline)
    (run_dir / "eval_report.md").write_text(report, encoding="utf-8")

    if not args.json:
        print(report)
    summary = {k: v for k, v in stats.items() if not k.startswith("_")}
    if baseline_stats:
        summary = {
            "current": summary,
            "baseline": {k: v for k, v in baseline_stats.items() if not k.startswith("_")},
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
