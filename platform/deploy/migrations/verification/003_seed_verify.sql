-- ============================================================================
-- FORGE · 003 最小验证数据（幂等）
-- ----------------------------------------------------------------------------
-- 目的：不是业务种子，而是隔离性验收数据 —— 两个租户，各自独立数据，
--       用于证明 RLS 边界在「跨租户」「跨供应商」「跨标注员」三个方向都成立。
--       业务种子仍由前端确定性种子 / Phase 4 导入流程提供。
-- ============================================================================

set search_path to forge, public;

-- 迁移与种子以超级用户执行（运维动作）；应用角色 forge_app 只做 DML 且受 RLS 约束。

insert into forge.enterprises (id, name, short_name, plan, seats) values
  ('ent_forge', '观澜数据科技有限公司', '观澜数据', 'internal', 240),
  ('ent_other', '示范租户 B（隔离测试）', '租户B', 'saas', 30)
on conflict (id) do nothing;

insert into forge.suppliers
  (id, enterprise_id, code, name, short_name, tier, status, modalities, city,
   capacity_hours, committed_hours, annotator_count, quality_score, on_time_rate, rework_rate,
   qual_business_license, qual_nda_signed, qual_security_audit)
values
  ('sup_a1', 'ent_forge', 'SUP-001', '上海语料工场信息科技', '上海语料', 'S', 'active', '{text,audio}', '上海',
   9600, 5400, 42, 91.8, 96.4, 2.4, true, true, true),
  ('sup_a2', 'ent_forge', 'SUP-005', '杭州视界标注中心', '杭州视界', 'S', 'active', '{image,video}', '杭州',
   10400, 9800, 51, 93.2, 97.1, 1.9, true, true, true),
  ('sup_b1', 'ent_other', 'SUP-901', '租户B的供应商', 'B供应', 'A', 'active', '{image}', '深圳',
   3000, 1200, 12, 85.0, 90.0, 4.0, true, true, false)
on conflict (id) do nothing;

insert into forge.supplier_members
  (id, enterprise_id, supplier_id, name, initials, role, skills, level, pass_rate, active_tasks)
values
  ('mem_a1', 'ent_forge', 'sup_a1', '朱承宇', '朱', 'annotator', '{text}', 'mid', 93.4, 2),
  ('mem_a2', 'ent_forge', 'sup_a1', '林佳怡', '林', 'annotator', '{text}', 'senior', 96.1, 1),
  ('mem_a3', 'ent_forge', 'sup_a2', '徐佳怡', '徐', 'annotator', '{image,video}', 'mid', 88.7, 3),
  ('mem_b1', 'ent_other', 'sup_b1', 'B租户标注员', 'B', 'annotator', '{image}', 'junior', 80.0, 1)
on conflict (id) do nothing;

insert into forge.projects
  (id, enterprise_id, code, name, client, modality, status, total_items, delivered_items,
   unit_price, budget, labeling_config_name, qc_sampling_rate)
values
  ('prj_a1', 'ent_forge', 'AD-2606', '自动驾驶多模态路采标注', '蔚途汽车', 'multimodal', 'running',
   186000, 107694, 4.20, 781200.00, 'Lane + Obstacle + Attribute', 12),
  ('prj_a2', 'ent_forge', 'EC-2607', '电商客服对话意图识别', '品汇电商', 'text', 'running',
   420000, 113400, 0.36, 151200.00, 'NER + Intent Classification', 5),
  ('prj_b1', 'ent_other', 'BX-9001', '租户B的项目', 'B客户', 'image', 'running',
   10000, 2000, 1.50, 15000.00, 'BBox', 10)
on conflict (id) do nothing;

insert into forge.project_suppliers (enterprise_id, project_id, supplier_id, quota_pct) values
  ('ent_forge', 'prj_a1', 'sup_a2', 100),
  ('ent_forge', 'prj_a2', 'sup_a1', 100),
  ('ent_other', 'prj_b1', 'sup_b1', 100)
on conflict do nothing;

insert into forge.tasks
  (id, enterprise_id, project_id, batch_no, name, modality, status, supplier_id, assignee_id,
   item_count, done_count, rework_round, unit_price, due_at, ls_project_id)
values
  ('tsk_a1', 'ent_forge', 'prj_a1', 'B01', 'AD-2606 · 批次 01', 'multimodal', 'in_progress', 'sup_a2', 'mem_a3',
   1500, 980, 0, 4.20, now() + interval '12 days', 214),
  ('tsk_a2', 'ent_forge', 'prj_a2', 'B01', 'EC-2607 · 批次 01', 'text', 'accepted', 'sup_a1', 'mem_a1',
   8000, 8000, 0, 0.36, now() - interval '2 days', 246),
  ('tsk_a3', 'ent_forge', 'prj_a2', 'B02', 'EC-2607 · 批次 02', 'text', 'rework', 'sup_a1', 'mem_a2',
   6000, 5100, 1, 0.36, now() + interval '5 days', 246),
  ('tsk_b1', 'ent_other', 'prj_b1', 'B01', 'BX-9001 · 批次 01', 'image', 'in_progress', 'sup_b1', 'mem_b1',
   900, 120, 0, 1.50, now() + interval '20 days', 301)
on conflict (id) do nothing;

insert into forge.settlements
  (id, enterprise_id, supplier_id, code, period, accepted_items, unit_price_avg,
   amount, deduction, payable, status, due_at)
values
  ('stl_a1', 'ent_forge', 'sup_a1', 'STL-202609-001', '2026-09', 92963, 2.41,
   224039.83, 7328.91, 216710.92, 'invoiced', now() + interval '10 days'),
  ('stl_a2', 'ent_forge', 'sup_a2', 'STL-202609-005', '2026-09', 66909, 2.68,
   179316.12, 6096.17, 173219.95, 'paid', now() - interval '5 days'),
  ('stl_b1', 'ent_other', 'sup_b1', 'STL-202609-901', '2026-09', 1200, 1.50,
   1800.00, 0.00, 1800.00, 'draft', now() + interval '30 days')
on conflict (id) do nothing;

insert into forge.reviews (id, enterprise_id, task_id, stage, result, sampled, defects, note, reviewer_id)
values
  ('rev_a1', 'ent_forge', 'tsk_a2', 'l1', 'pass', 400, 6, '抽检符合规范', 'usr_010'),
  ('rev_a2', 'ent_forge', 'tsk_a3', 'l1', 'rework', 300, 43, '存在漏标', 'usr_011'),
  ('rev_b1', 'ent_other', 'tsk_b1', 'l1', 'pass', 100, 2, 'B 租户审核', 'usr_b01')
on conflict (id) do nothing;

insert into forge.audit_log (enterprise_id, actor_name, action, target, detail)
select v.eid, v.actor, v.act, v.tgt, v.det
from (values
  ('ent_forge', '陆一鸣', 'project.create', '创建项目', 'AD-2606 立项'),
  ('ent_forge', '岑雨', 'review.rework', '裁定返工', 'EC-2607 · 批次 02 返工'),
  ('ent_other', 'B 管理员', 'project.create', '创建项目', 'BX-9001 立项')
) as v(eid, actor, act, tgt, det)
where not exists (
  select 1 from forge.audit_log a where a.enterprise_id = v.eid and a.action = v.act and a.target = v.tgt
);
