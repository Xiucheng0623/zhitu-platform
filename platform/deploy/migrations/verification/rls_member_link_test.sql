-- ============================================================================
-- 012 RLS 单测：标注员只见自己的批次与自己的成员行
-- ----------------------------------------------------------------------------
-- 跑法（以应用账号，不是超管 —— 超管绕过 RLS，测了等于没测）：
--   PGPASSWORD=<app密码> psql -U forge_app -d <库> -f rls_member_link_test.sql
--
-- 顺序讲究：测试数据必须在「管理视角」（assignee 上下文为空）下插入，
-- 因为 forge_app 的 INSERT 也要过 RLS 的 with check —— 带标注员上下文
-- 插别人的批次会被直接拒绝（这本身就是正确的生产行为）。
-- ============================================================================

\set ON_ERROR_STOP on

-- ---- 0. 管理视角（内部管理员：supplier 与 assignee 都为空）：建测试数据 ----
select set_config('forge.enterprise_id', 'ent_forge', false);
select set_config('forge.supplier_id', '', false);
select set_config('forge.assignee_id', '', false);

insert into forge.suppliers (id, enterprise_id, code, name, short_name, status)
values ('sup_t1', 'ent_forge', 'S-T1', '测试供应商甲', '甲', 'active')
on conflict (id) do nothing;

-- 两个花名册成员：m1 即将绑定登录账号、m2 不绑
insert into forge.supplier_members (id, enterprise_id, supplier_id, name, role)
values ('mem_t1', 'ent_forge', 'sup_t1', '张三', 'annotator'),
       ('mem_t2', 'ent_forge', 'sup_t1', '李四', 'annotator')
on conflict (id) do nothing;

insert into forge.users (id, enterprise_id, phone, password_hash, name, roles)
values ('usr_t1', 'ent_forge', '+8613900001111', 'x', '张三', array['annotator']),
       ('usr_t2', 'ent_forge', '+8613900002222', 'x', '李四', array['annotator'])
on conflict (id) do nothing;

update forge.supplier_members set user_id = 'usr_t1' where id = 'mem_t1';

insert into forge.projects (id, enterprise_id, code, name, client, modality, status)
values ('prj_t1', 'ent_forge', 'T-001', '测试项目', '测试甲方', 'text', 'running')
on conflict (id) do nothing;

-- 两个批次：tsk_t1 分给 m1（绑定 usr_t1），tsk_t2 分给 m2（未绑）
insert into forge.tasks (id, enterprise_id, project_id, batch_no, name, modality, status,
                         supplier_id, assignee_id, item_count)
values ('tsk_t1', 'ent_forge', 'prj_t1', 'B-001', '批次一', 'text', 'assigned',
        'sup_t1', 'mem_t1', 10),
       ('tsk_t2', 'ent_forge', 'prj_t1', 'B-002', '批次二', 'text', 'assigned',
        'sup_t1', 'mem_t2', 10)
on conflict (id) do nothing;

insert into forge.task_assignments (enterprise_id, task_id, supplier_id, assignee_id, assigned_by)
select 'ent_forge', 'tsk_t1', 'sup_t1', 'mem_t1', 'usr_admin'
where not exists (select 1 from forge.task_assignments where task_id = 'tsk_t1');
insert into forge.task_assignments (enterprise_id, task_id, supplier_id, assignee_id, assigned_by)
select 'ent_forge', 'tsk_t2', 'sup_t1', 'mem_t2', 'usr_admin'
where not exists (select 1 from forge.task_assignments where task_id = 'tsk_t2');

-- ---- 1. 标注员 usr_t1（绑定 mem_t1，供应商 sup_t1）：只见自己的 ----------
select set_config('forge.supplier_id', 'sup_t1', false);
select set_config('forge.assignee_id', 'usr_t1', false);
\echo '[期望 1] 标注员只见 tsk_t1（tasks=1, ids=tsk_t1）'
select count(*) as tasks_seen, string_agg(id, ',') as ids from forge.tasks;
\echo '[期望 2] 标注员只见自己成员行（members=1, ids=mem_t1）'
select count(*) as members_seen, string_agg(id, ',') as ids from forge.supplier_members;
\echo '[期望 3] 标注员只见自己的分配记录（assignments=1）'
select count(*) as assignments_seen from forge.task_assignments;

-- ---- 2. 供应商组长（assignee 空）：本公司全量 ---------------------------
select set_config('forge.assignee_id', '', false);
\echo '[期望 4] 组长见 2 个批次 / 2 名成员 / 2 条分配'
select count(*) as tasks_seen from forge.tasks;
select count(*) as members_seen from forge.supplier_members;
select count(*) as assignments_seen from forge.task_assignments;

-- ---- 3. 绑定即时生效：usr_t2 绑上 mem_t2 后马上可见 ---------------------
select set_config('forge.assignee_id', 'usr_t2', false);
\echo '[期望 5] usr_t2 未绑定时见 0 个批次'
select count(*) as tasks_seen from forge.tasks;

-- 注意：绑定动作本身也要在管理视角下做（标注员上下文改不了别人的行）
select set_config('forge.assignee_id', '', false);
update forge.supplier_members set user_id = 'usr_t2' where id = 'mem_t2';

select set_config('forge.assignee_id', 'usr_t2', false);
\echo '[期望 6] usr_t2 绑定后立即见到 tsk_t2（1 行，无需重登录）'
select count(*) as tasks_seen, string_agg(id, ',') as ids from forge.tasks;

\echo 'ALL RLS CHECKS DONE'
