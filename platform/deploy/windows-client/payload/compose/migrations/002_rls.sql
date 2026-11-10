-- ============================================================================
-- FORGE · 002 行级安全（RLS）
-- ----------------------------------------------------------------------------
-- 设计原则：每条策略用 AND 串联全部边界，绝不让多个 policy 形成 OR
--           导致边界被放宽（这是 RLS 最常见的实现事故）。
--
-- 会话上下文由应用在每个事务开始时设置：
--   select set_config('forge.enterprise_id', $1, true);
--   select set_config('forge.supplier_id',   $2, true);   -- 供应商侧主体才设
--   select set_config('forge.assignee_id',   $3, true);   -- 标注员主体才设
-- 第三参数 true = 事务级作用域，连接复用时不会串味。
-- ============================================================================

set search_path to forge, public;

-- 统一取上下文函数：空串与 null 都视为「未限定」
create or replace function forge.ctx(key text) returns text as $$
  select nullif(coalesce(current_setting(key, true), ''), '')
$$ language sql stable;

-- ---------------------------------------------------------------------------
-- 企业
-- ---------------------------------------------------------------------------
alter table forge.enterprises enable row level security;
alter table forge.enterprises force row level security;
drop policy if exists p_enterprises on forge.enterprises;
create policy p_enterprises on forge.enterprises
  for all
  using (id = forge.ctx('forge.enterprise_id'))
  with check (id = forge.ctx('forge.enterprise_id'));

-- ---------------------------------------------------------------------------
-- 供应商：租户 AND（未限定供应商 或 命中本供应商）
-- ---------------------------------------------------------------------------
alter table forge.suppliers enable row level security;
alter table forge.suppliers force row level security;
drop policy if exists p_suppliers on forge.suppliers;
create policy p_suppliers on forge.suppliers
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (forge.ctx('forge.supplier_id') is null or id = forge.ctx('forge.supplier_id'))
  );

-- ---------------------------------------------------------------------------
-- 供应商成员
-- ---------------------------------------------------------------------------
alter table forge.supplier_members enable row level security;
alter table forge.supplier_members force row level security;
drop policy if exists p_members on forge.supplier_members;
create policy p_members on forge.supplier_members
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (forge.ctx('forge.supplier_id') is null or supplier_id = forge.ctx('forge.supplier_id'))
  );

-- ---------------------------------------------------------------------------
-- 项目：供应商侧只能看到自己承接的项目
-- ---------------------------------------------------------------------------
alter table forge.projects enable row level security;
alter table forge.projects force row level security;
drop policy if exists p_projects on forge.projects;
create policy p_projects on forge.projects
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (
      forge.ctx('forge.supplier_id') is null
      or exists (
        select 1 from forge.project_suppliers ps
        where ps.project_id = projects.id
          and ps.supplier_id = forge.ctx('forge.supplier_id')
      )
    )
  );

alter table forge.project_suppliers enable row level security;
alter table forge.project_suppliers force row level security;
drop policy if exists p_ps on forge.project_suppliers;
create policy p_ps on forge.project_suppliers
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (forge.ctx('forge.supplier_id') is null or supplier_id = forge.ctx('forge.supplier_id'))
  );

-- ---------------------------------------------------------------------------
-- 任务：三道边界（租户 / 供应商 / 标注员）
-- ---------------------------------------------------------------------------
alter table forge.tasks enable row level security;
alter table forge.tasks force row level security;
drop policy if exists p_tasks on forge.tasks;
create policy p_tasks on forge.tasks
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (forge.ctx('forge.supplier_id') is null or supplier_id = forge.ctx('forge.supplier_id'))
    and (forge.ctx('forge.assignee_id') is null or assignee_id = forge.ctx('forge.assignee_id'))
  );

alter table forge.task_assignments enable row level security;
alter table forge.task_assignments force row level security;
drop policy if exists p_assignments on forge.task_assignments;
create policy p_assignments on forge.task_assignments
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (forge.ctx('forge.supplier_id') is null or supplier_id = forge.ctx('forge.supplier_id'))
  );

alter table forge.annotation_submissions enable row level security;
alter table forge.annotation_submissions force row level security;
drop policy if exists p_submissions on forge.annotation_submissions;
create policy p_submissions on forge.annotation_submissions
  for all
  using (enterprise_id = forge.ctx('forge.enterprise_id'))
  with check (enterprise_id = forge.ctx('forge.enterprise_id'));

-- ---------------------------------------------------------------------------
-- 审核：审核员只能看到自己经手或授权的记录 —— 租户级 + 任务级联
-- ---------------------------------------------------------------------------
alter table forge.reviews enable row level security;
alter table forge.reviews force row level security;
drop policy if exists p_reviews on forge.reviews;
create policy p_reviews on forge.reviews
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (
      forge.ctx('forge.supplier_id') is null
      or exists (
        select 1 from forge.tasks t
        where t.id = reviews.task_id
          and t.supplier_id = forge.ctx('forge.supplier_id')
      )
    )
  );

-- ---------------------------------------------------------------------------
-- 资产 / 交付 / 绩效 / 审计
-- ---------------------------------------------------------------------------
alter table forge.assets enable row level security;
alter table forge.assets force row level security;
drop policy if exists p_assets on forge.assets;
create policy p_assets on forge.assets
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (
      forge.ctx('forge.supplier_id') is null
      or exists (
        select 1 from forge.project_suppliers ps
        where ps.project_id = assets.project_id
          and ps.supplier_id = forge.ctx('forge.supplier_id')
      )
    )
  );

alter table forge.deliveries enable row level security;
alter table forge.deliveries force row level security;
drop policy if exists p_deliveries on forge.deliveries;
create policy p_deliveries on forge.deliveries
  for all
  using (enterprise_id = forge.ctx('forge.enterprise_id'))
  with check (enterprise_id = forge.ctx('forge.enterprise_id'));

alter table forge.settlements enable row level security;
alter table forge.settlements force row level security;
drop policy if exists p_settlements on forge.settlements;
create policy p_settlements on forge.settlements
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (forge.ctx('forge.supplier_id') is null or supplier_id = forge.ctx('forge.supplier_id'))
  );

alter table forge.performance_records enable row level security;
alter table forge.performance_records force row level security;
drop policy if exists p_perf on forge.performance_records;
create policy p_perf on forge.performance_records
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (forge.ctx('forge.supplier_id') is null or supplier_id = forge.ctx('forge.supplier_id'))
  );

alter table forge.audit_log enable row level security;
alter table forge.audit_log force row level security;
drop policy if exists p_audit on forge.audit_log;
create policy p_audit on forge.audit_log
  for select
  using (enterprise_id = forge.ctx('forge.enterprise_id'));
drop policy if exists p_audit_insert on forge.audit_log;
create policy p_audit_insert on forge.audit_log
  for insert
  with check (enterprise_id = forge.ctx('forge.enterprise_id'));

alter table forge.idempotency_keys enable row level security;
alter table forge.idempotency_keys force row level security;
drop policy if exists p_idem on forge.idempotency_keys;
create policy p_idem on forge.idempotency_keys
  for all
  using (enterprise_id = forge.ctx('forge.enterprise_id'))
  with check (enterprise_id = forge.ctx('forge.enterprise_id'));

-- ---------------------------------------------------------------------------
-- 视图也走 RLS（PG15+ security_invoker），否则视图会绕过策略
-- ---------------------------------------------------------------------------
alter view forge.v_project_progress set (security_invoker = on);

-- ---------------------------------------------------------------------------
-- 权限收敛：应用角色不得 DDL，不得绕过 RLS
-- ---------------------------------------------------------------------------
grant usage on schema forge to forge_app;
grant select, insert, update, delete on all tables in schema forge to forge_app;
grant usage, select on all sequences in schema forge to forge_app;
revoke create on schema forge from forge_app;
