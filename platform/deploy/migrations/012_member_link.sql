-- ============================================================================
-- FORGE · 012 成员-账号关联（消除标注员任务可见性的 ID 断层）
-- ----------------------------------------------------------------------------
-- 问题背景（2026-11-06 排查确认）：
--   任务分配的 assignee_id 存的是供应商花名册成员 id（supplier_members.id，
--   形如 mem_*），而标注员登录后 RLS 上下文 forge.assignee_id 里放的是
--   登录账号 id（forge.users.id，形如 usr_*）。两个体系永不相等，
--   002 的 p_tasks 策略 `assignee_id = forge.ctx('forge.assignee_id')`
--   会让标注员**恒查到 0 条任务** —— 功能表现为「我的任务永远是空的」。
--
-- 修法（两层，都不依赖 JWT 快照）：
--   1. supplier_members.user_id 建立「花名册成员 ↔ 登录账号」可选绑定。
--   2. RLS 策略的 assignee 匹配改为「id 直接相等 或 花名册成员绑定了该账号」，
--      映射在 SQL 层实时解析 —— 绑定/解绑立即生效，不需要重新登录。
--
-- 顺带收紧 p_members：标注员（assignee 上下文有值）只能看到自己的成员行，
-- 同供应商其他组员的花名册（技能、通过率、产能）不该对他可见。
-- 供应商组长/管理员的 assignee 上下文为空，仍可看本公司全量花名册。
-- ============================================================================

set search_path to forge, public;

-- ---------------------------------------------------------------------------
-- 1. 花名册成员 ↔ 登录账号（可空：不是每个花名册成员都有平台账号）
-- ---------------------------------------------------------------------------
alter table forge.supplier_members
  add column if not exists user_id text references forge.users(id) on delete set null;

-- 一个登录账号最多绑定一个花名册成员；没绑的行（null）不限数量
create unique index if not exists ux_supplier_members_user
  on forge.supplier_members (user_id)
  where user_id is not null;

comment on column forge.supplier_members.user_id is
  '绑定的平台登录账号（forge.users.id）。分配/结算按花名册成员算，'
  '登录与 RLS 按账号算，靠这一列把两个 id 体系接起来。可空 = 未开通登录。';

-- ---------------------------------------------------------------------------
-- 2. 任务可见性：标注员只见分配给自己的批次
--    assignee 语义兼容两种写法（直接存 usr_ 或存 mem_），以 mem_ 为正。
--
--    通道语义（012 修订版，实测教训）：
--      · assignee 上下文有值（标注员）→ 只走 assignee 通道，**不再叠
--        supplier 过滤**。因为 assignee 映射的子查询跑在 supplier_members
--        上，若 supplier 过滤同样生效，「账号归属供应商」与「花名册行
--        归属供应商」稍有不一致（换绑/迁移/数据对齐中）就会把人静默
--        清零成 0 条任务。分配给我的就是我的，与供应商归属无关。
--      · assignee 上下文为空 → 走 supplier 通道（供应商管理员本公司
--        全量）/ 都为空（内部人员）→ 全量。
-- ---------------------------------------------------------------------------
drop policy if exists p_tasks on forge.tasks;
create policy p_tasks on forge.tasks
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (
      (
        forge.ctx('forge.assignee_id') is not null
        and (
          assignee_id = forge.ctx('forge.assignee_id')
          or assignee_id in (
            select sm.id from forge.supplier_members sm
            where sm.user_id = forge.ctx('forge.assignee_id')
          )
        )
      )
      or (
        forge.ctx('forge.assignee_id') is null
        and (forge.ctx('forge.supplier_id') is null
             or supplier_id = forge.ctx('forge.supplier_id'))
      )
    )
  );

-- 分配历史同理：标注员只见自己的分配记录，不能翻同供应商谁分了什么
drop policy if exists p_assignments on forge.task_assignments;
create policy p_assignments on forge.task_assignments
  for all
  using (
    enterprise_id = forge.ctx('forge.enterprise_id')
    and (
      (
        forge.ctx('forge.assignee_id') is not null
        and (
          assignee_id = forge.ctx('forge.assignee_id')
          or assignee_id in (
            select sm.id from forge.supplier_members sm
            where sm.user_id = forge.ctx('forge.assignee_id')
          )
        )
      )
      or (
        forge.ctx('forge.assignee_id') is null
        and (forge.ctx('forge.supplier_id') is null
             or supplier_id = forge.ctx('forge.supplier_id'))
      )
    )
  );

-- ---------------------------------------------------------------------------
-- 3. 花名册可见性：标注员只见自己那一行
--    组长/管理员 assignee 上下文为空 → 本公司全量花名册（管人需要）。
--    注意：p_tasks 的 assignee 映射子查询也压在这张表的 RLS 上，
--    所以「标注员只见自己那一行」必须是 user_id 匹配就够——
--    这里若叠 supplier 过滤，映射子查询会被滤空，任务跟着清零（实测踩过）。
-- ---------------------------------------------------------------------------
drop policy if exists p_members on forge.supplier_members;
create policy p_members on forge.supplier_members
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
        and (forge.ctx('forge.supplier_id') is null
             or supplier_id = forge.ctx('forge.supplier_id'))
      )
    )
  );
