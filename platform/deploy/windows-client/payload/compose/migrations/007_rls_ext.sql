-- ============================================================================
-- FORGE · 007 行级安全扩展（财务域 + 采集域）
-- ----------------------------------------------------------------------------
-- 与 002_rls.sql 同一套写法：
--   - for all 只写 using，不叠加 with check（避免多 policy 形成 OR 放宽边界，
--     也避免把唯一约束错误误判成 RLS 生效 —— 这个坑在 004 测试里踩过）
--   - 每条策略用 AND 串联全部边界
--   - 视图必须 security_invoker，否则视图会绕过策略
--
-- 财务域的隔离比业务域更严：供应商只能看自己名下的发票与付款，
-- 但看不到 payment_allocations 上的其他结算单（那是别人的应收）。
-- ============================================================================

set search_path to forge, public;

-- ---------------------------------------------------------------------------
-- 发票
-- ---------------------------------------------------------------------------
alter table forge.invoices enable row level security;
alter table forge.invoices force row level security;
drop policy if exists p_invoices on forge.invoices;
create policy p_invoices on forge.invoices
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (forge.ctx('forge.supplier_id') is null or supplier_id = forge.ctx('forge.supplier_id'))
  );

-- ---------------------------------------------------------------------------
-- 付款单
-- ---------------------------------------------------------------------------
alter table forge.payments enable row level security;
alter table forge.payments force row level security;
drop policy if exists p_payments on forge.payments;
create policy p_payments on forge.payments
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (forge.ctx('forge.supplier_id') is null or supplier_id = forge.ctx('forge.supplier_id'))
  );

-- ---------------------------------------------------------------------------
-- 核销：以付款单的归属为准（核销行本身没有 supplier_id）
-- ---------------------------------------------------------------------------
alter table forge.payment_allocations enable row level security;
alter table forge.payment_allocations force row level security;
drop policy if exists p_allocations on forge.payment_allocations;
create policy p_allocations on forge.payment_allocations
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (
      forge.ctx('forge.supplier_id') is null
      or exists (
        select 1 from forge.payments pm
        where pm.id = payment_allocations.payment_id
          and pm.supplier_id = forge.ctx('forge.supplier_id')
      )
    )
  );

-- ---------------------------------------------------------------------------
-- 采集员
-- ---------------------------------------------------------------------------
alter table forge.collectors enable row level security;
alter table forge.collectors force row level security;
drop policy if exists p_collectors on forge.collectors;
create policy p_collectors on forge.collectors
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (forge.ctx('forge.supplier_id') is null or supplier_id = forge.ctx('forge.supplier_id'))
  );

-- ---------------------------------------------------------------------------
-- 采集任务：与项目同权（供应商只能看自己承接项目下的采集任务）
-- ---------------------------------------------------------------------------
alter table forge.collection_tasks enable row level security;
alter table forge.collection_tasks force row level security;
drop policy if exists p_ctasks on forge.collection_tasks;
create policy p_ctasks on forge.collection_tasks
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (
      forge.ctx('forge.supplier_id') is null
      or exists (
        select 1 from forge.project_suppliers ps
        where ps.project_id = collection_tasks.project_id
          and ps.supplier_id = forge.ctx('forge.supplier_id')
      )
    )
  );

-- ---------------------------------------------------------------------------
-- 采集脚本：继承任务的可见性
-- ---------------------------------------------------------------------------
alter table forge.collection_scripts enable row level security;
alter table forge.collection_scripts force row level security;
drop policy if exists p_cscripts on forge.collection_scripts;
create policy p_cscripts on forge.collection_scripts
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (
      forge.ctx('forge.supplier_id') is null
      or exists (
        select 1 from forge.collection_tasks t
          join forge.project_suppliers ps on ps.project_id = t.project_id
        where t.id = collection_scripts.task_id
          and ps.supplier_id = forge.ctx('forge.supplier_id')
      )
    )
  );

-- ---------------------------------------------------------------------------
-- 采集场次：供应商只看自己旗下采集员的场次
-- ---------------------------------------------------------------------------
alter table forge.collection_sessions enable row level security;
alter table forge.collection_sessions force row level security;
drop policy if exists p_csessions on forge.collection_sessions;
create policy p_csessions on forge.collection_sessions
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (
      forge.ctx('forge.supplier_id') is null
      or exists (
        select 1 from forge.collectors c
        where c.id = collection_sessions.collector_id
          and c.supplier_id = forge.ctx('forge.supplier_id')
      )
    )
  );

-- ---------------------------------------------------------------------------
-- 视图同样走 RLS
-- ---------------------------------------------------------------------------
alter view forge.v_settlement_match  set (security_invoker = on);
alter view forge.v_payables_aging    set (security_invoker = on);
alter view forge.v_collection_progress set (security_invoker = on);

-- ---------------------------------------------------------------------------
-- 权限：002 的 grant 只覆盖当时已存在的表，新表必须重新授权
-- ---------------------------------------------------------------------------
grant usage on schema forge to forge_app;
grant select, insert, update, delete on all tables in schema forge to forge_app;
grant usage, select on all sequences in schema forge to forge_app;
revoke create on schema forge from forge_app;
