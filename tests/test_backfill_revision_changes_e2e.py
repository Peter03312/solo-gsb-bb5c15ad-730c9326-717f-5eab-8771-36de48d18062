"""启动历史回填前态锚点修复的端到端验证（需真实 PostgreSQL）。

复现缺陷：修订 1 已封口（在线提交写入）、修订 2 撤回关键证据使关系变未定且
修订 2 待补（部署前历史、无封口标记）时，旧逻辑只取待补序列并把修订 2 与
**空库**比较——撤回的证据不在空前态里，撤回被漏记，revision_changes 中修订 2
没有任何行，成果包复核的“首次偏离”随之失真（找不到首次偏离修订）。

修复口径：
- 每个待补修订以**紧邻上一正式修订**（已封口或同样待补）的当时生效断言为
  前态、本修订为后态；零变化修订也封口；只补未封口修订；
- 重复启动不重写、不重复；回填与在线提交交错（在线提交抢先封口）不漏不重；
- 回填结果与在线提交口径一致（直接走在线路径产出的台账作为对照）。

运行：
    PYTHONPATH=/tmp/pylibs python3 tests/test_backfill_revision_changes_e2e.py
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pgserver  # type: ignore
    import psycopg2
except ImportError as exc:  # pragma: no cover
    print(f"SKIP: 缺少真实 PostgreSQL 验证依赖（{exc.name}）")
    sys.exit(0)

from app import db, service  # noqa: E402
from app.changes import backfill_revision_changes  # noqa: E402
from app.models import AssertOp, BatchIn, RetractOp  # noqa: E402

PGDATA = "/tmp/pgdata-backfill-e2e"

ALL_TABLES = (
    "evidence_hold_items", "evidence_holds",
    "field_package_corrections", "field_packages", "field_short_bindings",
    "thematic_packages", "snapshot_conclusions", "snapshots",
    "revision_changes_sealed", "revision_changes", "revisions",
    "projection_revisions", "projection_retractions", "projection_assertions",
    "projections", "assertions", "strata",
)


def start_server():
    os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-backfill")
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)
    os.chmod(os.environ["XDG_RUNTIME_DIR"], 0o700)
    srv = pgserver.get_server(PGDATA)
    uri = srv.get_uri()
    conn = psycopg2.connect(uri)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname='strat'")
        if cur.fetchone() is None:
            cur.execute("CREATE ROLE strat LOGIN PASSWORD 'strat' SUPERUSER")
        cur.execute("SELECT 1 FROM pg_database WHERE datname='stratigraphy'")
        if cur.fetchone() is None:
            cur.execute("CREATE DATABASE stratigraphy OWNER strat")
    conn.close()
    return srv


def reset_state():
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")
            cur.execute("UPDATE revision_counter SET revision = 0 WHERE id = TRUE")


def register_strata():
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            for s in ("甲", "乙", "丙"):
                cur.execute(
                    "INSERT INTO strata (stratum_id, site) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (s, "甲遗址"),
                )


def batch(expected: int, ops: list[dict]) -> int:
    operations = [
        AssertOp(**op) if op["op"] == "assert" else RetractOp(**op) for op in ops
    ]
    return service.apply_batch(BatchIn(expected_revision=expected, operations=operations))["revision"]


def change_rows():
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT revision, seq, stratum_a, stratum_b, old_relation, new_relation, "
                "old_path, new_path, source FROM revision_changes ORDER BY revision, seq"
            )
            return cur.fetchall()


def sealed_revisions():
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT revision, source FROM revision_changes_sealed ORDER BY revision")
            return cur.fetchall()


def unseal(*revisions: int):
    """模拟部署前历史：删掉封口标记，使这些修订成为待补（审计与断言保留）。"""
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM revision_changes_sealed WHERE revision = ANY(%s)", (list(revisions),))


def delete_change_rows(*revisions: int):
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM revision_changes WHERE revision = ANY(%s)", (list(revisions),))


# ------------------------------------------------------------------ 场景
def scenario_interleaved_sealed_pending():
    """修订 1 已封口、修订 2 撤回使“甲早于乙”变未定且待补：旧逻辑漏记撤回。"""
    reset_state()
    register_strata()
    # 修订 1：E1 甲 同期 丙；E2 丙 早于 乙 → 甲早于乙，最优路径 [E1, E2]
    r1 = batch(0, [
        {"op": "assert", "evidence_id": "E1", "stratum_a": "甲", "stratum_b": "丙", "relation": "same_period"},
        {"op": "assert", "evidence_id": "E2", "stratum_a": "丙", "stratum_b": "乙", "relation": "earlier_than"},
    ])
    assert r1 == 1
    # 修订 2：撤回 E2（唯一的早于桥）→ 甲对乙变未定
    r2 = batch(1, [{"op": "retract", "evidence_id": "E2"}])
    assert r2 == 2

    online_rows = change_rows()
    assert len(online_rows) == 5, online_rows  # 修订1 三对；修订2 两对
    row_r2 = {(r[2], r[3]): r for r in online_rows if r[0] == 2}
    # 撤回唯一早于桥 E2：丙-乙 与 乙-甲（甲早于乙）均变未定
    assert set(row_r2) == {("丙", "乙"), ("乙", "甲")}, row_r2
    key_pair = row_r2[("乙", "甲")]
    assert key_pair[4] == "later_than" and key_pair[5] == "undetermined", key_pair
    assert list(key_pair[6]) == ["E1", "E2"] and list(key_pair[7]) == [], key_pair

    # 模拟部署前历史：修订 2 的封口与变化记录缺失（待补），修订 1 保持已封口
    delete_change_rows(2)
    unseal(2)
    assert {r[0] for r in sealed_revisions()} == {1}
    assert [r for r in change_rows() if r[0] == 2] == []

    with db.db_conn() as conn:
        with conn.cursor() as cur:
            n = backfill_revision_changes(cur)
    assert n == 1, f"应只回填修订 2，实际 {n}"

    rows = change_rows()
    assert len(rows) == 5, rows
    backfilled = {(r[2], r[3]): r for r in rows if r[0] == 2}
    assert set(backfilled) == {("丙", "乙"), ("乙", "甲")}, backfilled
    r = backfilled[("乙", "甲")]
    assert (r[2], r[3], r[4], r[5]) == ("乙", "甲", "later_than", "undetermined"), r
    assert list(r[6]) == ["E1", "E2"], r  # 前态取修订 1 当时状态：撤回被正确记录
    assert list(r[7]) == [], r
    assert r[8] == "formal_batch", r
    other = backfilled[("丙", "乙")]
    assert (other[4], other[5], list(other[6]), list(other[7])) == (
        "earlier_than", "undetermined", ["E2"], []
    ), other
    # 修订 1 的在线记录原样保留（前态空库的新增记录），未被重写
    r1_rows = [r for r in rows if r[0] == 1]
    assert len(r1_rows) == 3, r1_rows
    assert {r[0] for r in sealed_revisions()} == {1, 2}
    print("PASS 场景1：已封口/待补交错——待补修订以前一正式修订为前态，撤回不漏记")
    return online_rows, rows


def scenario_two_consecutive_pending_with_sealed_head():
    """修订 1 已封口，修订 2、3 连续待补：修订 3 的前态必须是修订 2 而非空库。"""
    reset_state()
    register_strata()
    r1 = batch(0, [
        {"op": "assert", "evidence_id": "E1", "stratum_a": "甲", "stratum_b": "乙", "relation": "earlier_than"},
    ])
    r2 = batch(1, [
        {"op": "assert", "evidence_id": "E2", "stratum_a": "乙", "stratum_b": "丙", "relation": "earlier_than"},
    ])
    # 修订 3：撤回 E1。此时 甲-乙 变未定；甲-丙 也变未定（原路径 [E1,E2] 断裂）
    r3 = batch(2, [{"op": "retract", "evidence_id": "E1"}])
    assert (r1, r2, r3) == (1, 2, 3)

    online_rows = change_rows()
    delete_change_rows(2, 3)
    unseal(2, 3)
    assert {r[0] for r in sealed_revisions()} == {1}

    with db.db_conn() as conn:
        with conn.cursor() as cur:
            n = backfill_revision_changes(cur)
    assert n == 2, n
    rows = change_rows()
    assert [r for r in rows if r[0] == 2] == [r for r in online_rows if r[0] == 2]
    # 修订 3 前态必须是修订 2（含 E1、E2）：甲-乙 earlier_than→undetermined、
    # 甲-丙 earlier_than→undetermined 两条；若错与空库比较则两条全漏。
    r3_rows = sorted((r[2], r[3], r[4], r[5]) for r in rows if r[0] == 3)
    assert r3_rows == [
        ("丙", "甲", "later_than", "undetermined"),
        ("乙", "甲", "later_than", "undetermined"),
    ], r3_rows
    for r in rows:
        if r[0] == 3:
            assert list(r[6])  # 旧路径非空——证明前态锚定到修订 2
    assert {r[0] for r in sealed_revisions()} == {1, 2, 3}
    print("PASS 场景2：连续多个待补修订——前态沿正式修订链推进，不错位到空库")


def scenario_zero_change_pending_gets_sealed():
    """零变化的待补修订也要封口（不产生变化行，但重复启动不再处理）。"""
    reset_state()
    register_strata()
    batch(0, [
        {"op": "assert", "evidence_id": "E1", "stratum_a": "甲", "stratum_b": "乙", "relation": "earlier_than"},
    ])
    # 修订 2：新登记 丁/戊 并断言同期（产生丁-戊这一新对的变化）
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO strata (stratum_id) VALUES ('丁'), ('戊') ON CONFLICT DO NOTHING")
    batch(1, [
        {"op": "assert", "evidence_id": "E9", "stratum_a": "丁", "stratum_b": "戊", "relation": "same_period"},
    ])
    # 修订 3（真正零变化）：再加一条丁同期戊的平行证据——结论不变，
    # 最优单证据路径为 [E9]（编号 E9 < EZ1），路径也不变。
    batch(2, [
        {"op": "assert", "evidence_id": "EZ1", "stratum_a": "丁", "stratum_b": "戊", "relation": "same_period"},
    ])
    online = change_rows()
    zero_revision = 3
    assert not [r for r in online if r[0] == zero_revision], online

    delete_change_rows(2, 3)
    unseal(2, 3)
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            assert backfill_revision_changes(cur) == 2
    assert {r[0] for r in sealed_revisions()} == {1, 2, 3}
    assert not [r for r in change_rows() if r[0] == zero_revision]
    # 重复启动：零变化修订不再被当作待补
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            assert backfill_revision_changes(cur) == 0
    print("PASS 场景3：零变化待补修订只封口、无变化行；重复启动不处理")


def scenario_repeated_startup_idempotent():
    """重复启动：已封口修订不重写，台账与封口表逐字节不变。"""
    reset_state()
    register_strata()
    batch(0, [
        {"op": "assert", "evidence_id": "E1", "stratum_a": "甲", "stratum_b": "乙", "relation": "earlier_than"},
    ])
    batch(1, [{"op": "retract", "evidence_id": "E1"}])
    unseal(1, 2)
    delete_change_rows(1, 2)

    with db.db_conn() as conn:
        with conn.cursor() as cur:
            assert backfill_revision_changes(cur) == 2
    rows_once = change_rows()
    sealed_once = sealed_revisions()
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            assert backfill_revision_changes(cur) == 0
            assert backfill_revision_changes(cur) == 0
    assert change_rows() == rows_once
    assert sealed_revisions() == sealed_once
    print("PASS 场景4：重复启动幂等——不重写、不漏记、不重复")


def scenario_interleave_with_online_commit():
    """回填与在线提交交错：在线提交在回填读取待补列表后抢先封口修订 2。

    用两个连接模拟：连接 A 持有修订计数器行锁开始回填事务，连接 B 的在线
    提交阻塞等待行锁；A 提交释放后 B 完成。两个事务对修订 2 只能有一个
    写入封口（PRIMARY KEY 兜底），最终不重、不漏。
    """
    reset_state()
    register_strata()
    batch(0, [
        {"op": "assert", "evidence_id": "E1", "stratum_a": "甲", "stratum_b": "乙", "relation": "earlier_than"},
    ])
    batch(1, [{"op": "retract", "evidence_id": "E1"}])
    unseal(1, 2)
    delete_change_rows(1, 2)

    raw_conn = db._pool.getconn()
    error_box: list[Exception] = []

    def online_commit():
        try:
            # 修订 3 的在线提交（与回填争同一行锁；在 A 提交后才执行）
            r = batch(2, [
                {"op": "assert", "evidence_id": "E3", "stratum_a": "乙", "stratum_b": "丙",
                 "relation": "earlier_than"},
            ])
            assert r == 3
        except Exception as exc:  # noqa: BLE001
            error_box.append(exc)

    try:
        cur = raw_conn.cursor()
        cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
        cur.execute("SELECT revision FROM revision_changes_sealed")
        sealed = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT revision, applied_at, batch FROM revisions ORDER BY revision")
        pending = [r for r in cur.fetchall() if r[0] not in sealed]

        t = threading.Thread(target=online_commit)
        t.start()
        # 回填事务持锁期间，在线提交被行锁阻塞，无法抢先封口
        threading.Event().wait(1.0)

        # 直接在持锁事务内执行回填主体（与启动路径同一函数无法重入池连接，
        # 这里复刻：在线提交此时不可能提交，故 pending 快照稳定）
        from app.changes import _active_assertions_at, relation_diffs, insert_revision_changes
        cur.execute("SELECT stratum_id, created_at FROM strata")
        strata_created = {r[0]: r[1] for r in cur.fetchall()}
        prev = None
        for revision, applied_at, batch_json in pending:
            before = _active_assertions_at(cur, prev)
            after = _active_assertions_at(cur, revision)
            strata = [s for s, created in strata_created.items() if created <= applied_at]
            diffs = relation_diffs(strata, before, after)
            insert_revision_changes(cur, revision, diffs, "formal_batch")
            prev = revision
        raw_conn.commit()  # 释放行锁
        t.join(timeout=10)
        assert not t.is_alive(), "在线提交线程未结束"
        assert not error_box, error_box
    finally:
        db._pool.putconn(raw_conn)

    assert {r[0] for r in sealed_revisions()} == {1, 2, 3}
    by_rev: dict[int, int] = {}
    for r in change_rows():
        by_rev[r[0]] = by_rev.get(r[0], 0) + 1
    assert by_rev.get(1) == 1 and by_rev.get(2) == 1 and by_rev.get(3) == 1, by_rev
    # 再来一轮启动回填：什么都不做
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            assert backfill_revision_changes(cur) == 0
    assert {r[0] for r in sealed_revisions()} == {1, 2, 3}
    print("PASS 场景5：回填与在线提交交错——行锁互斥 + 封口兜底，不重不漏")


def scenario_equivalence_with_online_path():
    """回填全部修订的结果必须与全走在线路径的台账逐行一致（含来源）。"""
    # 自包含构造两修订库，随后删除全部封口与台账整体回填
    reset_state()
    register_strata()
    batch(0, [
        {"op": "assert", "evidence_id": "E1", "stratum_a": "甲", "stratum_b": "丙", "relation": "same_period"},
        {"op": "assert", "evidence_id": "E2", "stratum_a": "丙", "stratum_b": "乙", "relation": "earlier_than"},
    ])
    batch(1, [{"op": "retract", "evidence_id": "E2"}])
    online = change_rows()
    unseal(1, 2)
    delete_change_rows(1, 2)
    assert sealed_revisions() == [] and change_rows() == []

    with db.db_conn() as conn:
        with conn.cursor() as cur:
            assert backfill_revision_changes(cur) == 2
    refilled = change_rows()
    assert refilled == online, (refilled, online)

    # 重复回填不再产生任何变化
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            assert backfill_revision_changes(cur) == 0
    assert change_rows() == online
    print("PASS 场景6：全量回填与在线提交流程逐行一致（结论/路径/来源/顺序）")


def main() -> None:
    srv = start_server()
    # srv.get_uri() 形如 postgresql://postgres:@/postgres?host=/tmp/pgdata-xxx
    from urllib.parse import urlparse, parse_qs

    parsed = urlparse(srv.get_uri())
    host = parse_qs(parsed.query)["host"][0]
    os.environ["DATABASE_URL"] = f"postgresql://strat:strat@/stratigraphy?host={host}"
    db.DATABASE_URL = os.environ["DATABASE_URL"]
    db.init_pool(retries=10, delay=1.0)  # 全新库：启动回填空跑

    scenario_interleaved_sealed_pending()
    scenario_two_consecutive_pending_with_sealed_head()
    scenario_zero_change_pending_gets_sealed()
    scenario_repeated_startup_idempotent()
    scenario_interleave_with_online_commit()
    scenario_equivalence_with_online_path()
    print("ALL BACKFILL SCENARIOS PASSED")


if __name__ == "__main__":
    main()
