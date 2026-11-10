-- ============================================================================
-- FORGE · 013 标注工作台 UX 增强（规范侧栏 + 单题耗时）
-- ----------------------------------------------------------------------------
-- 解决两件事：
--   1. 标注员在标注界面右侧要看到「本项目的标注规范与示例图」。
--      Label Studio CE 没有插件机制（官方 Plugins 是企业版独占），规范内容
--      只能存平台侧、由编排层提供只读接口，LS 注入层拉取渲染。存平台而不是
--      存 LS 的项目描述字段：LS 那边是纯文本、无权限、改完没人审。
--
--   2. 单题耗时回传。这是**计价与效率模型的原始事实**，不是装饰指标：
--      单价谈判要「人均日产出」，返工判断要「异常慢的题」。
--
-- 为什么规范单独建表、不放在 forge.projects 上（重要，别改回去）：
--   标注员的 Principal 带 supplier_id，而 p_projects 只认 project_suppliers
--   承接关系 —— 顺手把 projects 对标注员打开，等于把 budget / unit_price
--   这些商务字段一起给了他。行级权限没有列级粒度，所以把「可公开给一线」
--   的规范和「商务敏感」的项目本体拆成两张表，各自定策略，这才是干净解法。
--
-- 设计取舍：
--   * 只存 ls_task_id（LS 自己的 task id），不复制题目内容。标注结果的正本
--     永远在 LS，平台只存引用（同 annotation_submissions 的口径）。
--   * 题型/批次用 ls_project_id 反查 forge.tasks（建项目时就是按批次建的，
--     1:1）。反查不到就留 null，不编造归属。
--   * 单题时长上限 4 小时（14400 秒）。挂机、忘了关页面、午饭回来接着点，
--     都会污染均值；宁可丢弃也不能让"人均 6 小时一题"进计价模型。
-- ============================================================================

set search_path to forge, public;

-- ---------------------------------------------------------------------------
-- 0. 清理上一版草稿：曾把规范放在 projects 的两个列上，已废弃
--    （本迁移未发布，直接原地清理，保持幂等）
-- ---------------------------------------------------------------------------
alter table forge.projects drop column if exists guideline_md;
alter table forge.projects drop column if exists guideline_images;

-- ---------------------------------------------------------------------------
-- 1. 项目级标注规范
-- ---------------------------------------------------------------------------
create table if not exists forge.project_guidelines (
  project_id    text primary key references forge.projects(id) on delete cascade,
  enterprise_id text not null references forge.enterprises(id) on delete restrict,
  -- Markdown 原样存，渲染在前端做：存 HTML 会把 XSS 面一起存进库里
  markdown      text not null default '',
  -- 示例图 URL（按顺序展示）。只允许 http/https 绝对地址，写入时校验。
  images        text[] not null default '{}',
  updated_by    text,
  updated_at    timestamptz not null default now()
);
create index if not exists ix_guidelines_tenant on forge.project_guidelines (enterprise_id);

comment on table forge.project_guidelines is
  '项目级标注规范（右侧侧栏数据源）。与 forge.projects 分表：'
  '本表要开放给一线标注员读，projects 里有 budget/unit_price 不能开放。';

alter table forge.project_guidelines enable row level security;
alter table forge.project_guidelines force row level security;
drop policy if exists p_guidelines on forge.project_guidelines;
create policy p_guidelines on forge.project_guidelines
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (
      -- 内部人员（两个上下文都为空）：全量，要维护规范
      (
        forge.ctx('forge.supplier_id') is null
        and forge.ctx('forge.assignee_id') is null
      )
      -- 供应商：自己承接的项目
      or exists (
        select 1 from forge.project_suppliers ps
        where ps.project_id = project_guidelines.project_id
          and ps.supplier_id = forge.ctx('forge.supplier_id')
      )
      -- 有任务在手的人（标注员）：p_tasks 已把 tasks 收窄到"分配给自己的"，
      -- 所以这里的 EXISTS 一旦成立，就证明该行任务对他可见。
      -- 读的是规范，不是项目商务字段，不存在越权泄密面。
      or exists (
        select 1 from forge.tasks t
        where t.project_id = project_guidelines.project_id
      )
    )
  )
  with check (
    enterprise_id = forge.ctx('forge.enterprise_id')
    -- 写入只给内部人员。标注员/供应商即使看得到规范也不能改：
    -- rbac 里 project:write 本来也只发给 platform_admin / ops_director /
    -- project_manager，这里做第二道兜底。
    and forge.ctx('forge.supplier_id') is null
    and forge.ctx('forge.assignee_id') is null
  );

-- ---------------------------------------------------------------------------
-- 2. 单题耗时打点
-- ---------------------------------------------------------------------------
create table if not exists forge.task_time_logs (
  id             bigserial primary key,
  enterprise_id  text not null references forge.enterprises(id) on delete restrict,
  -- 由 ls_project_id 反查得到的平台批次；查不到就是 null（不猜）
  task_id        text references forge.tasks(id) on delete cascade,
  -- 产生这条记录的登录账号。写入方是 LS 注入层，凭据是编排层签的短期上下文，
  -- 所以这个值可信；但仍只作为「归属」使用，不作为「权限」。
  user_id        text not null references forge.users(id) on delete cascade,
  ls_project_id  integer not null,
  ls_task_id     integer,
  event          text not null check (event in ('enter', 'leave', 'submit')),
  duration_ms    integer check (duration_ms is null or duration_ms between 0 and 14400000),
  queue_position integer,
  queue_total    integer,
  client_at      timestamptz,
  created_at     timestamptz not null default now()
);

create index if not exists ix_ttl_task on forge.task_time_logs (task_id, created_at desc);
create index if not exists ix_ttl_user on forge.task_time_logs (enterprise_id, user_id, created_at desc);
create index if not exists ix_ttl_ls on forge.task_time_logs (ls_project_id, ls_task_id);

comment on table forge.task_time_logs is
  '单题标注耗时流水。event 三态：enter（进入该题）/ leave（离开该题，带时长）'
  '/ submit（提交成功）。时长只在 leave / submit 上有值。';

-- ---------------------------------------------------------------------------
-- 3. RLS：按角色收口
--    · 标注员（assignee 上下文有值）→ 只看自己产生的行
--    · 供应商侧（supplier 上下文有值）→ 看本公司账号产生的行
--    · 内部人员（两个上下文都为空）→ 全量（要算产能与计价）
--    插入的 with check 与 using 同构，多一条「归属必须是发起人」：
--    标注员只能往自己名下写，避免前端被改造成给同事刷工时。
-- ---------------------------------------------------------------------------
alter table forge.task_time_logs enable row level security;
alter table forge.task_time_logs force row level security;

drop policy if exists p_ttl on forge.task_time_logs;
create policy p_ttl on forge.task_time_logs
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (
      (
        forge.ctx('forge.assignee_id') is not null
        and user_id = forge.ctx('forge.assignee_id')
      )
      or (
        forge.ctx('forge.assignee_id') is null
        and forge.ctx('forge.supplier_id') is not null
        and user_id in (
          select sm.user_id from forge.supplier_members sm
          where sm.supplier_id = forge.ctx('forge.supplier_id')
            and sm.user_id is not null
        )
      )
      or (
        forge.ctx('forge.assignee_id') is null
        and forge.ctx('forge.supplier_id') is null
      )
    )
  )
  with check (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (
      forge.ctx('forge.assignee_id') is null
      or user_id = forge.ctx('forge.assignee_id')
    )
  );

-- ---------------------------------------------------------------------------
-- 4. 授权
--    002 用的是 `grant ... on all tables`，新表不会被自动包含（010 踩过），
--    所以这里必须显式补一次；序列也要补（bigserial 的 insert 依赖它）。
-- ---------------------------------------------------------------------------
grant usage on schema forge to forge_app;
grant select, insert, update, delete on forge.project_guidelines to forge_app;
grant select, insert on forge.task_time_logs to forge_app;
grant usage, select on sequence forge.task_time_logs_id_seq to forge_app;
-- 时长流水只增不改：不 grant update / delete。写错了靠新增一条纠正，
-- 否则"改工时"会变成一条悄无声息的旁路。
