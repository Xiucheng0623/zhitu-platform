-- ============================================================================
-- FORGE · 001 结构迁移（幂等，可重复执行）
-- ----------------------------------------------------------------------------
-- 约定：
--   1. 主键用 text（nanoid/uuid 由应用生成），不用 serial，避免分布式写入冲突
--   2. 每张业务表必带 enterprise_id，作为多租户第一道边界
--   3. 金额一律 numeric(14,2)，禁止 float
--   4. 时间一律 timestamptz，服务端 UTC 存储
--   5. 枚举用 CHECK 约束而非 enum type，便于灰度新增取值
--   6. 只增不改：列新增用 add column if not exists，不做破坏性变更
-- ============================================================================

create schema if not exists forge;
set search_path to forge, public;

-- 触发器：更新 updated_at
create or replace function forge.touch_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

-- ---------------------------------------------------------------------------
-- 1. 企业（租户）
-- ---------------------------------------------------------------------------
create table if not exists forge.enterprises (
  id            text primary key,
  name          text not null,
  short_name    text not null,
  plan          text not null default 'internal' check (plan in ('internal','saas')),
  seats         integer not null default 10 check (seats > 0),
  data_region   text not null default 'cn-east-1',
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 2. 供应商
-- ---------------------------------------------------------------------------
create table if not exists forge.suppliers (
  id                  text primary key,
  enterprise_id       text not null references forge.enterprises(id) on delete restrict,
  code                text not null,
  name                text not null,
  short_name          text not null,
  tier                text not null default 'C' check (tier in ('S','A','B','C')),
  status              text not null default 'qualifying'
                      check (status in ('active','probation','qualifying','suspended')),
  modalities          text[] not null default '{}',
  city                text,
  contact_name        text,
  contact_phone       text,
  capacity_hours      integer not null default 0 check (capacity_hours >= 0),
  committed_hours     integer not null default 0 check (committed_hours >= 0),
  annotator_count     integer not null default 0,
  quality_score       numeric(4,1) not null default 0 check (quality_score between 0 and 100),
  on_time_rate        numeric(4,1) not null default 0 check (on_time_rate between 0 and 100),
  rework_rate         numeric(4,1) not null default 0 check (rework_rate between 0 and 100),
  contract_count      integer not null default 0,
  onboarded_at        timestamptz,
  qual_business_license boolean not null default false,
  qual_nda_signed       boolean not null default false,
  qual_security_audit   boolean not null default false,
  qual_last_audited_at  timestamptz,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);
create unique index if not exists ux_suppliers_tenant_code on forge.suppliers (enterprise_id, code);
create index if not exists ix_suppliers_tenant_status on forge.suppliers (enterprise_id, status);
create index if not exists ix_suppliers_tier on forge.suppliers (enterprise_id, tier);

-- ---------------------------------------------------------------------------
-- 3. 供应商成员（标注员 / 组长 / 供应商管理员）
-- ---------------------------------------------------------------------------
create table if not exists forge.supplier_members (
  id              text primary key,
  enterprise_id   text not null references forge.enterprises(id) on delete restrict,
  supplier_id     text not null references forge.suppliers(id) on delete cascade,
  name            text not null,
  initials        text,
  role            text not null default 'annotator'
                  check (role in ('supplier_admin','supplier_lead','annotator')),
  skills          text[] not null default '{}',
  level           text not null default 'mid' check (level in ('senior','mid','junior')),
  status          text not null default 'active' check (status in ('active','inactive')),
  pass_rate       numeric(4,1) not null default 0,
  completed_total integer not null default 0,
  active_tasks    integer not null default 0,
  joined_at       timestamptz not null default now(),
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index if not exists ix_members_supplier on forge.supplier_members (supplier_id, status);
create index if not exists ix_members_tenant on forge.supplier_members (enterprise_id);

-- ---------------------------------------------------------------------------
-- 4. 项目
-- ---------------------------------------------------------------------------
create table if not exists forge.projects (
  id                    text primary key,
  enterprise_id         text not null references forge.enterprises(id) on delete restrict,
  code                  text not null,
  name                  text not null,
  client                text not null,
  modality              text not null
                        check (modality in ('image','video','audio','text','multimodal')),
  status                text not null default 'draft'
                        check (status in ('draft','planning','running','paused','accepting','closed')),
  manager_id            text,
  total_items           integer not null default 0 check (total_items >= 0),
  delivered_items       integer not null default 0 check (delivered_items >= 0),
  start_at              timestamptz,
  due_at                timestamptz,
  unit_price            numeric(10,2) not null default 0 check (unit_price >= 0),
  budget                numeric(14,2) not null default 0,
  pass_rate             numeric(4,1) not null default 0,
  labeling_config_id    text,
  labeling_config_name  text,
  qc_sampling_rate      integer not null default 10 check (qc_sampling_rate between 0 and 100),
  max_rework            integer not null default 3,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  constraint ck_delivered_le_total check (delivered_items <= total_items * 1.05)
);
create unique index if not exists ux_projects_tenant_code on forge.projects (enterprise_id, code);
create index if not exists ix_projects_tenant_status on forge.projects (enterprise_id, status);
create index if not exists ix_projects_due on forge.projects (enterprise_id, due_at);

-- ---------------------------------------------------------------------------
-- 5. 项目 ↔ 供应商（承接关系，多对多）
-- ---------------------------------------------------------------------------
create table if not exists forge.project_suppliers (
  enterprise_id text not null references forge.enterprises(id) on delete restrict,
  project_id    text not null references forge.projects(id) on delete cascade,
  supplier_id   text not null references forge.suppliers(id) on delete restrict,
  quota_pct     numeric(5,2) not null default 0 check (quota_pct between 0 and 100),
  created_at    timestamptz not null default now(),
  primary key (project_id, supplier_id)
);
create index if not exists ix_ps_supplier on forge.project_suppliers (supplier_id);

-- ---------------------------------------------------------------------------
-- 6. 标注任务（批次）
-- ---------------------------------------------------------------------------
create table if not exists forge.tasks (
  id            text primary key,
  enterprise_id text not null references forge.enterprises(id) on delete restrict,
  project_id    text not null references forge.projects(id) on delete cascade,
  batch_no      text not null,
  name          text not null,
  modality      text not null check (modality in ('image','video','audio','text','multimodal')),
  status        text not null default 'pending'
                check (status in ('pending','assigned','in_progress','submitted','reviewing','rework','accepted','rejected')),
  supplier_id   text references forge.suppliers(id) on delete restrict,
  assignee_id   text references forge.supplier_members(id) on delete set null,
  item_count    integer not null default 0 check (item_count >= 0),
  done_count    integer not null default 0 check (done_count >= 0),
  rework_round  integer not null default 0 check (rework_round >= 0),
  unit_price    numeric(10,2) not null default 0,
  due_at        timestamptz,
  ls_project_id integer,
  priority      text not null default 'normal' check (priority in ('high','normal','low')),
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  constraint ck_done_le_items check (done_count <= item_count),
  constraint ck_assigned_has_supplier check (status <> 'assigned' or supplier_id is not null)
);
create unique index if not exists ux_tasks_project_batch on forge.tasks (project_id, batch_no);
create index if not exists ix_tasks_tenant_status on forge.tasks (enterprise_id, status);
create index if not exists ix_tasks_supplier on forge.tasks (supplier_id, status);
create index if not exists ix_tasks_assignee on forge.tasks (assignee_id, status);
create index if not exists ix_tasks_due on forge.tasks (enterprise_id, due_at) where status not in ('accepted','rejected');

-- ---------------------------------------------------------------------------
-- 7. 任务分配历史（审计要求：可追溯每次改派）
-- ---------------------------------------------------------------------------
create table if not exists forge.task_assignments (
  id            bigserial primary key,
  enterprise_id text not null,
  task_id       text not null references forge.tasks(id) on delete cascade,
  supplier_id   text not null references forge.suppliers(id),
  assignee_id   text not null references forge.supplier_members(id),
  assigned_by   text not null,
  assigned_at   timestamptz not null default now(),
  unassigned_at timestamptz
);
create index if not exists ix_ta_task on forge.task_assignments (task_id, assigned_at desc);

-- ---------------------------------------------------------------------------
-- 8. 标注提交（引用 Label Studio，不复制标注结果）
-- ---------------------------------------------------------------------------
create table if not exists forge.annotation_submissions (
  id                text primary key,
  enterprise_id     text not null,
  task_id           text not null references forge.tasks(id) on delete cascade,
  submitter_id      text not null references forge.supplier_members(id),
  item_count        integer not null default 0,
  ls_project_id     integer,
  ls_annotation_ref text,
  content_hash      text,
  status            text not null default 'submitted'
                    check (status in ('submitted','accepted','rework','rejected')),
  submitted_at      timestamptz not null default now()
);
create index if not exists ix_sub_task on forge.annotation_submissions (task_id, submitted_at desc);

-- ---------------------------------------------------------------------------
-- 9. 审核记录
-- ---------------------------------------------------------------------------
create table if not exists forge.reviews (
  id            text primary key,
  enterprise_id text not null,
  task_id       text not null references forge.tasks(id) on delete cascade,
  stage         text not null check (stage in ('l1','l2','qc')),
  result        text not null check (result in ('pass','rework','reject')),
  sampled       integer not null default 0 check (sampled >= 0),
  defects       integer not null default 0 check (defects >= 0),
  note          text,
  reviewer_id   text not null,
  reviewed_at   timestamptz not null default now()
);
create index if not exists ix_reviews_task on forge.reviews (task_id, reviewed_at desc);
create index if not exists ix_reviews_tenant_time on forge.reviews (enterprise_id, reviewed_at desc);

-- ---------------------------------------------------------------------------
-- 10. 数据资产（批次入湖）
-- ---------------------------------------------------------------------------
create table if not exists forge.assets (
  id            text primary key,
  enterprise_id text not null,
  project_id    text not null references forge.projects(id) on delete cascade,
  batch_no      text not null,
  name          text not null,
  modality      text not null check (modality in ('image','video','audio','text','multimodal')),
  status        text not null default 'raw'
                check (status in ('raw','in_annotation','in_review','accepted','archived')),
  size_mb       integer not null default 0,
  item_count    integer not null default 0,
  storage_key   text,
  qc_score      numeric(4,1),
  imported_at   timestamptz not null default now(),
  created_at    timestamptz not null default now()
);
create index if not exists ix_assets_project on forge.assets (project_id, status);
create unique index if not exists ux_assets_project_batch on forge.assets (project_id, batch_no, name);

-- ---------------------------------------------------------------------------
-- 11. 交付记录
-- ---------------------------------------------------------------------------
create table if not exists forge.deliveries (
  id              text primary key,
  enterprise_id   text not null,
  project_id      text not null references forge.projects(id) on delete cascade,
  version         text not null,
  format          text not null,
  item_count      integer not null default 0,
  object_key      text,
  delivered_at    timestamptz not null default now(),
  accepted_at     timestamptz,
  client_sign_off boolean not null default false
);
create index if not exists ix_deliveries_project on forge.deliveries (project_id, delivered_at desc);

-- ---------------------------------------------------------------------------
-- 12. 结算单
-- ---------------------------------------------------------------------------
create table if not exists forge.settlements (
  id               text primary key,
  enterprise_id    text not null references forge.enterprises(id) on delete restrict,
  supplier_id      text not null references forge.suppliers(id) on delete restrict,
  code             text not null,
  period           text not null check (period ~ '^\d{4}-\d{2}$'),
  accepted_items   integer not null default 0 check (accepted_items >= 0),
  unit_price_avg   numeric(10,4) not null default 0,
  amount           numeric(14,2) not null default 0,
  deduction        numeric(14,2) not null default 0 check (deduction >= 0),
  payable          numeric(14,2) not null default 0,
  status           text not null default 'draft'
                   check (status in ('draft','submitted','confirmed','invoiced','paid')),
  purchase_order   text,
  purchase_invoice text,
  payment_entry    text,
  created_at       timestamptz not null default now(),
  due_at           timestamptz,
  updated_at       timestamptz not null default now(),
  constraint ck_settle_payable check (payable = amount - deduction)
);
create unique index if not exists ux_settle_supplier_period on forge.settlements (enterprise_id, supplier_id, period);
create index if not exists ix_settle_status on forge.settlements (enterprise_id, status);

-- ---------------------------------------------------------------------------
-- 13. 绩效记录（按期间快照，不覆盖历史）
-- ---------------------------------------------------------------------------
create table if not exists forge.performance_records (
  id            text primary key,
  enterprise_id text not null,
  supplier_id   text not null references forge.suppliers(id) on delete cascade,
  period        text not null check (period ~ '^\d{4}-\d{2}$'),
  quality_score numeric(4,1) not null default 0,
  on_time_rate  numeric(4,1) not null default 0,
  rework_rate   numeric(4,1) not null default 0,
  throughput    integer not null default 0,
  composite     numeric(4,1) not null default 0,
  rank          integer not null default 0,
  created_at    timestamptz not null default now()
);
create unique index if not exists ux_perf_supplier_period on forge.performance_records (supplier_id, period);

-- ---------------------------------------------------------------------------
-- 14. 审计日志（只追加）
-- ---------------------------------------------------------------------------
create table if not exists forge.audit_log (
  id            bigserial primary key,
  enterprise_id text not null,
  actor_id      text,
  actor_name    text not null,
  action        text not null,
  target        text,
  detail        text,
  ip            inet,
  at            timestamptz not null default now()
);
create index if not exists ix_audit_tenant_time on forge.audit_log (enterprise_id, at desc);
create index if not exists ix_audit_action on forge.audit_log (enterprise_id, action);

-- 禁止修改 / 删除审计日志
create or replace function forge.audit_immutable() returns trigger as $$
begin
  raise exception 'audit_log is append-only';
end;
$$ language plpgsql;

drop trigger if exists trg_audit_no_update on forge.audit_log;
create trigger trg_audit_no_update before update or delete on forge.audit_log
  for each row execute function forge.audit_immutable();

-- ---------------------------------------------------------------------------
-- 15. 幂等键（写操作重放保护）
-- ---------------------------------------------------------------------------
create table if not exists forge.idempotency_keys (
  key           text primary key,
  enterprise_id text not null,
  actor_id      text,
  method        text not null,
  path          text not null,
  status_code   integer not null,
  response      jsonb not null,
  created_at    timestamptz not null default now()
);
create index if not exists ix_idem_created on forge.idempotency_keys (created_at);

-- ---------------------------------------------------------------------------
-- 触发器绑定
-- ---------------------------------------------------------------------------
do $$
declare t text;
begin
  foreach t in array array['enterprises','suppliers','supplier_members','projects','tasks','settlements']
  loop
    execute format('drop trigger if exists trg_touch_%1$s on forge.%1$s', t);
    execute format('create trigger trg_touch_%1$s before update on forge.%1$s
                    for each row execute function forge.touch_updated_at()', t);
  end loop;
end $$;

-- ---------------------------------------------------------------------------
-- 视图：项目进度（只读聚合，前端与报表共用）
-- ---------------------------------------------------------------------------
create or replace view forge.v_project_progress as
select
  p.enterprise_id,
  p.id                as project_id,
  p.code,
  p.name,
  p.status,
  p.total_items,
  p.delivered_items,
  round(p.delivered_items::numeric / nullif(p.total_items, 0) * 100, 2) as progress_pct,
  (select count(*) from forge.tasks t where t.project_id = p.id)              as task_count,
  (select count(*) from forge.tasks t where t.project_id = p.id and t.status = 'accepted') as accepted_tasks,
  (select count(*) from forge.tasks t
     where t.project_id = p.id and t.status not in ('accepted','rejected') and t.due_at < now()) as overdue_tasks,
  p.due_at
from forge.projects p;
