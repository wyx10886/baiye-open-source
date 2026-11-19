"""Reproducible, anonymized acceptance scenarios for the local memory system.

This measures retrieval and persistence behavior, not human-like intelligence.
Every database lives in a TemporaryDirectory and is removed after the run.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from local_memory.store import Store


def evaluate():
    outcomes = []
    retrieval = []

    def check(name, tests, evidence):
        outcomes.append({"scenario": name, "passed": all(tests.values()),
                         "checks": tests, "evidence": evidence})

    def recall(label, rows, expected):
        hit = expected in {row["id"] for row in rows[:3]}
        retrieval.append({"query": label, "top3_hit": hit})
        return hit

    with tempfile.TemporaryDirectory(prefix="local-evaluation-") as folder:
        home = Path(folder)
        store = Store(home)

        def add(title, content, **kwargs):
            fields = dict(kind="event", title=title, content=content,
                          source="anonymized acceptance fixture: explicit collaboration evidence",
                          event_date=date.today().isoformat())
            fields.update(kwargs)
            return store.remember(**fields)

        # 1. A completely fresh Python process must find the persisted result.
        persisted = add("电池项目核查成果", "电池项目时间间隔已核查，下一步确认能量单位。", scope="project:battery")
        code = ("import json,sys; from local_memory.store import Store; "
                "print(json.dumps(Store(sys.argv[1]).search('时间间隔',scope='project:battery'),ensure_ascii=False))")
        child = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(home)],
                               cwd=Path(__file__).resolve().parent, encoding="utf-8", capture_output=True,
                               timeout=30, check=True)
        found = json.loads(child.stdout)
        check("新进程接续已保存成果", {"new_process_top3": recall("时间间隔", found, persisted["id"])},
              "子进程退出后重新打开同一SQLite，找到带来源的已确认核查成果。")

        # 2. Revision is a current-fact replacement with historical traceability.
        old = add("用户称呼偏好", "称呼使用示例甲。", kind="preference", key="address")
        new = store.revise(old["id"], expected_revision=old["revision"],
                           source="anonymized fixture: user explicitly corrected preference",
                           content="称呼改为示例乙；旧称呼停止使用。")
        found = store.search("称呼 示例乙")
        check("纠正偏好不复活旧版本", {
            "new_version_top3": recall("称呼 示例乙", found, new["id"]),
            "old_not_current": old["id"] not in {row["id"] for row in store.search("称呼")},
            "old_is_historical": store.get(old["id"])["state"] == "superseded",
        }, "当前检索命中新偏好；旧偏好只作为superseded历史记录保留。")

        # 3. Candidate proposal does not turn into a user decision or cross projects.
        decision = add("太阳能项目当前阶段", "研究路线尚未锁定，继续比较方案。", kind="project", scope="project:solar")
        proposal = add("太阳能候选路线", "考虑概率预测；用户尚未选择。", status="candidate", scope="project:solar")
        unrelated = add("预算项目下一步", "研究预算需要核对凭证。", kind="project", scope="project:budget")
        context = store.context("研究路线 概率预测", project="solar")
        confirmed_ids = {row["id"] for row in context["confirmed"]}
        check("项目隔离和候选状态", {
            "current_state_top3": recall("研究路线", context["confirmed"], decision["id"]),
            "proposal_only_candidate": proposal["id"] not in confirmed_ids
                and proposal["id"] in {row["id"] for row in context["candidates"]},
            "other_project_absent": unrelated["id"] not in confirmed_ids,
        }, "研究项目上下文区分当前阶段和未采纳建议，排除预算项目。")

        # 4. Fictional persona is opt-in data, never a scientific credential.
        persona = add("虚构角色背景", "虚构角色是故事讲述者；仅用于合成场景。", kind="persona")
        default = store.context("角色 讲述者")
        opted_in = store.search("角色 讲述者", include_persona=True)
        check("虚构角色与现实协作分开", {
            "persona_not_default": persona["id"] not in {row["id"] for row in default["confirmed"]},
            "persona_opt_in_top3": recall("角色 讲述者（显式启用）", opted_in, persona["id"]),
        }, "默认工作上下文排除persona，显式启用时可以查询。")

        # 5. Selected project baseline survives vague follow-up wording.
        next_step = add("水处理项目接续", "已完成字段核查。下一步：确认流量单位，再训练基线模型。",
                        kind="project", scope="project:water", key="current-state")
        context = store.context("我们接着往下做", project="water")
        check("模糊续聊仍带上下一步", {
            "next_step_top3": recall("我们接着往下做", context["confirmed"], next_step["id"]),
            "next_step_visible": "确认流量单位" in context["markdown"],
        }, "选定项目的当前状态作为基线上下文保留，不依赖提问复述原句。")

        # 6. Crucial provenance is current project state with searchable synonyms.
        provenance = add("样本来源和计量疑点", "样本为模拟数据；删除合成标志不改变来源，不能称为实测。计量单位仍待核实。",
                         kind="project", scope="project:solar", key="provenance",
                         tags=["模拟", "合成", "实测", "真实来源", "计量单位"])
        found = store.search("真实来源 实测", scope="project:solar")
        check("数据来源不会被标记删除误改", {
            "provenance_top3": recall("真实来源 实测", found, provenance["id"]),
            "source_still_synthetic": "模拟数据" in store.get(provenance["id"])["content"],
        }, "来源疑点以project状态保存并标注检索同义词；这仍是词法检索。")

        # 7. Separate Store instances simulate concurrent chats sharing one home.
        def writer(index):
            local = Store(home)
            return local.remember(kind="event", title=f"并行成果{index}", content=f"第{index}个独立成果已经核查。",
                                  source="anonymized concurrent fixture", scope="project:parallel", key=f"result-{index}")
        with ThreadPoolExecutor(max_workers=8) as pool:
            written = list(pool.map(writer, range(32)))
        check("多聊天并行记录不丢失", {
            "all_writes_returned": len({row["id"] for row in written}) == 32,
            "all_writes_persisted": len(store.list_memories(scope="project:parallel", limit=100)) == 32,
        }, "8个并发写入者分别创建Store，32条独立成果完整落库。")

        # 8. Restore reproduces a snapshot and creates a safety copy first.
        before = store.stats()["active"]
        snapshot = store.backup()
        added = add("备份之后的临时成果", "此项仅用于恢复验收。")
        result = store.restore(snapshot)
        active = store.list_memories(limit=1000)
        safety = Path(result["safety_backup"])
        check("备份恢复和恢复前保护", {
            "snapshot_restored": len(active) == before and added["id"] not in {row["id"] for row in active},
            "safety_backup_exists": safety.is_file(),
        }, "恢复到备份时点；恢复前状态另存为安全备份。恢复期间应暂停其他写入。")

    passed = sum(row["passed"] for row in outcomes)
    hits = sum(row["top3_hit"] for row in retrieval)
    return {
        "suite": "local-memory-anonymized-acceptance-v1",
        "date": date.today().isoformat(), "scenarios_passed": passed, "scenarios_total": len(outcomes),
        "all_passed": passed == len(outcomes),
        "retrieval": {"top3_hits": hits, "queries": len(retrieval), "top3_recall": round(hits / len(retrieval), 4),
                      "cases": retrieval},
        "outcomes": outcomes,
        "limits": ["小型人工构造场景不能代表所有真实对话。", "检索采用中英文词法匹配和项目状态基线，没有语义向量模型。",
                   "候选回合摘要需要助手或用户核验后才能升级为已确认。", "恢复备份会回到历史快照，操作期间应暂停其他写入。"],
    }


def markdown(report):
    lines = ["# 本地协作记忆：匿名场景验收", "", f"日期：{report['date']}", "",
             f"通过：{report['scenarios_passed']}/{report['scenarios_total']}个场景；"
             f"指定记忆Top-3命中：{report['retrieval']['top3_hits']}/{report['retrieval']['queries']}。", "",
             "所有案例均为人工构造的匿名协作资料，使用临时数据库；不含用户个人记忆。", "",
             "| 场景 | 结果 | 实际检查 |", "| --- | --- | --- |"]
    for row in report["outcomes"]:
        lines.append(f"| {row['scenario']} | {'通过' if row['passed'] else '未通过'} | {row['evidence']} |")
    lines.extend(["", "## 实际限制", ""] + [f"- {item}" for item in report["limits"]])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional report directory; source personal data is never read")
    args = parser.parse_args(argv)
    report = evaluate()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        stem = f"本地协作记忆_验收结果_{report['date']}"
        (args.output / f"{stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.output / f"{stem}.md").write_text(markdown(report), encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
