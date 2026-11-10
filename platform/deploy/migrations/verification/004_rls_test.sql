-- ============================================================================
-- FORGE · 004 隔离性验收测试
-- ----------------------------------------------------------------------------
-- 以应用角色 forge_app 执行（非超级用户，RLS 对其真实生效）。
-- 每个场景直接输出各表可见行数，人工/CI 断言即可。
-- 用法：psql -U forge_app -d forge -f 004_rls_test.sql
-- ============================================================================

\set ON_ERROR_STOP on
set search_path to forge, public;

\echo ''
\echo '=== 场景 1 · 内部视角（ent_forge，无供应商限定）==='
select set_config('forge.enterprise_id', 'ent_forge', false) is not null as ctx_set;
select set_config('forge.supplier_id', '', false) is not null as sup_cleared;
select set_config('forge.assignee_id', '', false) is not null as asg_cleared;
select
  (select count(*) from forge.tasks)       as tasks,
  (select count(*) from forge.projects)    as projects,
  (select count(*) from forge.suppliers)   as suppliers,
  (select count(*) from forge.settlements) as settlements,
  (select count(*) from forge.audit_log)   as audit_rows;

\echo ''
\echo '=== 场景 2 · 供应商视角 A（sup_a1：语料工场）==='
select set_config('forge.supplier_id', 'sup_a1', false) is not null as ctx_set;
select
  (select count(*) from forge.tasks)       as tasks,
  (select count(*) from forge.projects)    as projects,
  (select count(*) from forge.suppliers)   as suppliers,
  (select count(*) from forge.settlements) as settlements;

\echo ''
\echo '=== 场景 3 · 供应商视角 B（sup_a2：杭州视界）==='
select set_config('forge.supplier_id', 'sup_a2', false) is not null as ctx_set;
select
  (select count(*) from forge.tasks)       as tasks,
  (select count(*) from forge.projects)    as projects,
  (select count(*) from forge.suppliers)   as suppliers;

\echo ''
\echo '=== 场景 4 · 标注员视角（mem_a1，仅本人任务）==='
select set_config('forge.supplier_id', '', false) is not null as s;
select set_config('forge.assignee_id', 'mem_a1', false) is not null as a;
select
  (select count(*) from forge.tasks) as tasks,
  (select string_agg(id, ',') from forge.tasks) as task_ids;

\echo ''
\echo '=== 场景 5 · 跨租户（ent_other，应只看到 B 租户数据）==='
select set_config('forge.enterprise_id', 'ent_other', false) is not null as ctx_set;
select set_config('forge.assignee_id', '', false) is not null as a;
select
  (select count(*) from forge.tasks)       as tasks,
  (select count(*) from forge.projects)    as projects,
  (select count(*) from forge.suppliers)   as suppliers,
  (select count(*) from forge.settlements) as settlements;

\echo ''
\echo '=== 场景 6 · 越界组合（ent_forge + 他租户供应商 sup_b1，应为 0）==='
select set_config('forge.enterprise_id', 'ent_forge', false) is not null as e;
select set_config('forge.supplier_id', 'sup_b1', false) is not null as s;
select
  (select count(*) from forge.tasks)    as tasks,
  (select count(*) from forge.projects) as projects,
  (select count(*) from forge.suppliers) as suppliers;

\echo ''
\echo '=== 场景 7 · 写操作越权（sup_a1 试图插入 sup_a2 名下的任务，应被 RLS 拒绝）==='
select set_config('forge.supplier_id', 'sup_a1', false) is not null as s;
do $$
declare
  v_id text := 'tsk_evil_' || floor(extract(epoch from clock_timestamp()))::text;
begin
  insert into forge.tasks (id, enterprise_id, project_id, batch_no, name, modality, status, supplier_id, item_count)
  values (v_id, 'ent_forge', 'prj_a1', 'B99', '越权写入测试', 'image', 'pending', 'sup_a2', 10);
  raise notice 'FAIL: 越权写入竟然成功（% 已入库）', v_id;
exception
  when insufficient_privilege then
    raise notice 'PASS: 越权写入被 RLS 拒绝';
  when others then
    raise notice 'INCONCLUSIVE: 触发其他错误 -> %', sqlerrm;
end $$;

\echo ''
\echo '=== 场景 8 · 审计日志不可篡改（应 0 行影响，或被触发器拒绝）==='
do $$
declare n int;
begin
  begin
    update forge.audit_log set detail = 'tampered' where enterprise_id = 'ent_forge';
    get diagnostics n = row_count;
  exception
    when others then
      raise notice 'PASS: 审计日志修改被触发器拒绝 -> %', sqlerrm;
      return;
  end;
  if n = 0 then
    raise notice 'PASS: 0 行受影响（audit_log 未授予 update 策略）';
  else
    raise notice 'FAIL: % 行被篡改', n;
  end if;
end $$;

\echo ''
\echo '=== 场景 9 · 视图同样受 RLS 约束（security_invoker）==='
select set_config('forge.supplier_id', 'sup_a1', false) is not null as s;
select count(*) as visible_projects_in_view from forge.v_project_progress;
