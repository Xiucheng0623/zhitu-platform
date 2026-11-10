-- ===========================================================================
-- 014 · 标注流程（甲方可编辑的"员工怎么标"）
-- ===========================================================================
-- 为什么单独一张表，而不是挂在 forge.projects 上：
--   1. 这张表要开放给一线标注员/供应商读（标注页要展示"这一步做什么"），
--      而 projects 里有 budget / unit_price / pass_rate，不能开放；
--   2. 流程配置是"随版本演进"的文档型字段（steps jsonb），与项目的商务列
--      生命周期不同：项目可能半年不动，流程会改十次。
--   与 013 的 project_guidelines 同理，两张表都是"项目级内容资产"。
--
-- 两个字段的分工（别混用）：
--   steps       设计器的结构化数据（前端编辑用，可以带 UI 状态）
--   label_config 由 steps 生成/或直通手写的 XML（真正写进 Label Studio 的东西）
-- 为什么同时存：甲方手写 XML 时 steps 表达不了（我们只认 8 种控件），
-- 这时 label_config 是正本、steps 留空；反过来结构化编辑时 steps 是正本。
-- 只有一个字段就必然在"改了一半"的状态下丢东西。
-- ===========================================================================

create table if not exists forge.project_flows (
  project_id    text primary key references forge.projects(id) on delete cascade,
  enterprise_id text not null references forge.enterprises(id) on delete restrict,
  -- 结构化步骤：{"object":"image","steps":[...]}
  steps         jsonb not null default '{}'::jsonb,
  -- 生成或直通的 label_config XML 正本
  label_config  text not null default '',
  -- 来自哪个内置模板（仅作溯源，不参与渲染）
  template_key  text,
  -- 手写标记：true 表示 steps 与 label_config 已经不同步，设计器要只读提示
  raw_override  boolean not null default false,
  updated_by    text,
  updated_at    timestamptz not null default now()
);
create index if not exists ix_flows_tenant on forge.project_flows (enterprise_id);

comment on table forge.project_flows is
  '项目级标注流程（步骤 + label_config）。steps 供平台设计器编辑，'
  'label_config 是写进 Label Studio 的正本；raw_override=true 时以 label_config 为准。';

alter table forge.project_flows enable row level security;
alter table forge.project_flows force row level security;
drop policy if exists p_flows on forge.project_flows;
create policy p_flows on forge.project_flows
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (
      -- 内部人员：全量
      (
        forge.ctx('forge.supplier_id') is null
        and forge.ctx('forge.assignee_id') is null
      )
      -- 供应商：自己承接的项目
      or exists (
        select 1 from forge.project_suppliers ps
        where ps.project_id = project_flows.project_id
          and ps.supplier_id = forge.ctx('forge.supplier_id')
      )
      -- 标注员：手上有该项目的任务（p_tasks 已把 tasks 收窄到"分配给我的"）
      -- 读的是"这一步该做什么"，不含商务信息，不存在越权泄密面。
      or exists (
        select 1 from forge.tasks t
        where t.project_id = project_flows.project_id
      )
    )
  )
  with check (
    enterprise_id = forge.ctx('forge.enterprise_id')
    -- 写入只给内部人员：改流程等于改所有人的作业方式，不能让供应商自助改。
    and forge.ctx('forge.supplier_id') is null
    and forge.ctx('forge.assignee_id') is null
  );

grant select, insert, update, delete on forge.project_flows to forge_app;
