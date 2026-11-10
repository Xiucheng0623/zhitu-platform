# -*- coding: utf-8 -*-
"""清空 Label Studio 里的演示项目 / 任务 / 标注，保留用户、组织与 API token。

保留 htx_user / authtoken_token：账号是身份，不是数据；
编排层的服务账号 token 也在其中，删了会打断平台 → LS 的调用。
"""
from __future__ import annotations

import sqlite3
import sys

DB = r"D:/forge-ls/data/label_studio.sqlite3"

# 子表在前：标注草稿/锁/提交历史/视图/过滤器/webhook 都挂在 project 或 task 上
CLEAR = [
    "tasks_annotationdraft", "tasks_tasklock", "task_completion",
    "projects_labelstreamhistory", "projects_projectsummary",
    "data_manager_filtergroup", "data_manager_view",
    "webhook", "task", "project",
]


def main() -> int:
    dry = "--dry-run" in sys.argv
    con = sqlite3.connect(DB)
    con.execute("pragma foreign_keys = ON")
    cur = con.cursor()
    for t in CLEAR:
        try:
            cur.execute(f'select count(*) from "{t}"')
            n = cur.fetchone()[0]
        except sqlite3.OperationalError:
            print(f"skip    {t} (表不存在)")
            continue
        if not n:
            print(f"skip    {t:32s} 已是空表")
            continue
        if dry:
            print(f"would   {t:32s} {n} 行")
        else:
            cur.execute(f'delete from "{t}"')
            print(f"cleared {t:32s} {cur.rowcount}")
    if dry:
        con.rollback()
        print("\n--dry-run：已回滚，未改动")
        return 0
    con.commit()

    for t in ("project", "task", "htx_user", "authtoken_token", "organization"):
        cur.execute(f'select count(*) from "{t}"')
        print(f"  仍有数据 {t}: {cur.fetchone()[0]}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
