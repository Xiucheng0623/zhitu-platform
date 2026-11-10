-- ============================================================================
-- FORGE · 008 财务域 / 采集域 行为验证券
-- ----------------------------------------------------------------------------
-- 这个文件不测 UI，只测「库里的会计与采集约束是否真的会拦住错误」。
-- 每一段都做真写入，最后整体 rollback —— 可重复执行，不污染业务数据。
--
-- 用法：psql -U forge_app -d forge -f 008_finance_verify.sql
--       期望输出里全部是 PASS / INFO，不允许出现 FAIL。
-- ============================================================================

\set ON_ERROR_STOP on
set search_path to forge, public;

begin;

select set_config('forge.enterprise_id', 'ent_forge', true) is not null as ctx;
select set_config('forge.supplier_id', '', true) is not null as sup;
select set_config('forge.assignee_id', '', true) is not null as asg;

\echo ''
\echo '=== 场景 1 · 三单匹配视图能正确识别「缺发票」==='
select settlement_id, payable, invoice_count, invoiced_amount, paid_amount, match_state
  from forge.v_settlement_match
 where settlement_id = 'stl_a1';

\echo ''
\echo '=== 场景 2 · 发票价税合计必须自洽（故意写错，应被 CHECK 拒绝）==='
do $$
begin
  insert into forge.invoices
    (id, enterprise_id, supplier_id, settlement_id, code, invoice_no,
     invoice_type, amount, tax_rate, tax_amount, total, created_by)
  values ('tst_inv_bad', 'ent_forge', 'sup_a1', 'stl_a1', 'INV-BAD', 'BAD-0001',
          'vat_special', 1000.00, 0.06, 60.00, 9999.00, 'tester');
  raise notice 'FAIL: 价税不自洽的发票竟然入库了';
exception
  when check_violation then raise notice 'PASS: 价税合计不自洽被 CHECK 拒绝';
  when others then raise notice 'FAIL: 错误类型不对 -> %', sqlerrm;
end $$;

\echo ''
\echo '=== 场景 3 · 正常开票（204444.26 + 6% = 216710.92，正好等于应付）==='
insert into forge.invoices
  (id, enterprise_id, supplier_id, settlement_id, code, invoice_no,
   invoice_type, amount, tax_rate, tax_amount, total, status, issued_at, received_at, created_by)
values ('tst_inv_ok', 'ent_forge', 'sup_a1', 'stl_a1', 'INV-A1-001', '044001900111',
        'vat_special', 204444.26, 0.0600, 12266.66, 216710.92,
        'received', now(), now(), 'tester');

select code, amount, tax_amount, total, status from forge.invoices where id = 'tst_inv_ok';

\echo ''
\echo '=== 场景 4 · 同一供应商同一发票号不得重复（防重复报销）==='
do $$
begin
  insert into forge.invoices
    (id, enterprise_id, supplier_id, settlement_id, code, invoice_no,
     invoice_type, amount, tax_rate, tax_amount, total, created_by)
  values ('tst_inv_dup', 'ent_forge', 'sup_a1', 'stl_a1', 'INV-A1-002', '044001900111',
          'vat_special', 1000.00, 0.0600, 60.00, 1060.00, 'tester');
  raise notice 'FAIL: 重复发票号竟然入库了';
exception
  when unique_violation then raise notice 'PASS: 重复发票号被唯一索引拒绝';
  when others then raise notice 'FAIL: 错误类型不对 -> %', sqlerrm;
end $$;

\echo ''
\echo '=== 场景 5 · 发票核验后，匹配状态应为「可付款」==='
update forge.invoices
   set status = 'verified', verified_by = 'tester', verified_at = now()
 where id = 'tst_inv_ok';

select settlement_id, payable, invoiced_amount, verified_amount, paid_amount, match_state
  from forge.v_settlement_match
 where settlement_id = 'stl_a1';

\echo ''
\echo '=== 场景 6 · 付款单核销后，结算单状态应由核销自动推导为 paid ==='
insert into forge.payments
  (id, enterprise_id, supplier_id, code, amount, method, payee_name,
   payee_bank, payee_account, voucher_no, status,
   requested_by, approved_by, approved_at, paid_at)
values ('tst_pay_1', 'ent_forge', 'sup_a1', 'PAY-T-001', 216710.92, 'bank_transfer',
        '上海语料工场信息科技', '招商银行上海分行', '6225****1234', 'VCH-20260930-001',
        'succeeded', 'tester', 'tester', now(), now());

insert into forge.payment_allocations
  (enterprise_id, payment_id, settlement_id, amount, created_by)
values ('ent_forge', 'tst_pay_1', 'stl_a1', 216710.92, 'tester');

select s.status as settlement_status, m.paid_amount, m.outstanding, m.match_state
  from forge.settlements s
  join forge.v_settlement_match m on m.settlement_id = s.id
 where s.id = 'stl_a1';

\echo ''
\echo '=== 场景 7 · 超付必须被拒绝（再核销 1 元）==='
do $$
begin
  insert into forge.payment_allocations
    (enterprise_id, payment_id, settlement_id, amount, created_by)
  values ('ent_forge', 'tst_pay_1', 'stl_a1', 1.00, 'tester');
  raise notice 'FAIL: 超付竟然被允许';
exception
  when check_violation then raise notice 'PASS: 超付被触发器拒绝 -> %', sqlerrm;
  when others then raise notice 'FAIL: 错误类型不对 -> %', sqlerrm;
end $$;

\echo ''
\echo '=== 场景 8 · 付款被冲销后，结算单应退回 invoiced（不允许状态先行）==='
update forge.payments set status = 'reversed' where id = 'tst_pay_1';
select status as settlement_status from forge.settlements where id = 'stl_a1';

\echo ''
\echo '=== 场景 9 · 采集员未实名 / 未签协议不得上岗 ==='
do $$
begin
  insert into forge.collectors
    (id, enterprise_id, code, name, status, id_verified, agreement_signed)
  values ('tst_col_bad', 'ent_forge', 'COL-BAD', '测试采集员', 'active', false, true);
  raise notice 'FAIL: 未实名的采集员被置为 active';
exception
  when check_violation then raise notice 'PASS: 未实名采集员上岗被 CHECK 拒绝';
  when others then raise notice 'FAIL: 错误类型不对 -> %', sqlerrm;
end $$;

\echo ''
\echo '=== 场景 10 · 采集场次通过后回写任务进度与采集员累计 ==='
insert into forge.collectors
  (id, enterprise_id, code, name, city, accents, skills, status, id_verified, agreement_signed)
values ('tst_col_ok', 'ent_forge', 'COL-OK', '张采集', '上海', '{吴语,普通话}',
        '{audio,video}', 'active', true, true);

insert into forge.collection_tasks
  (id, enterprise_id, project_id, code, name, modality, scene, script_type,
   target_sessions, target_valid_clips, status, unit_price, budget)
values ('tst_ct_1', 'ent_forge', 'prj_a1', 'CT-T-001', '车内噪声语音采集', 'audio',
        '车内怠速/行驶', 'read', 2, 100, 'running', 3.5, 5000.00);

insert into forge.collection_sessions
  (id, enterprise_id, task_id, collector_id, site, city, device, sample_rate, channels,
   started_at, ended_at, duration_min, clip_count, valid_clip_count, rejected_clip_count,
   qc_status, status, payout)
values ('tst_cs_1', 'ent_forge', 'tst_ct_1', 'tst_col_ok', '张江基地 A2', '上海',
        'Zoom H6 + 领夹麦', 48000, 1, now() - interval '2 hours', now() - interval '1 hour',
        60, 60, 55, 5, 'passed', 'accepted', 192.50);

select task_id, done_sessions, valid_clips, spent, session_pct, clip_pct
  from forge.v_collection_progress where task_id = 'tst_ct_1';
select id, sessions_done, valid_clips from forge.collectors where id = 'tst_col_ok';

\echo ''
\echo '=== 场景 11 · 同一采集员不得同时有两场进行中的采集 ==='
do $$
begin
  insert into forge.collection_sessions
    (id, enterprise_id, task_id, collector_id, status)
  values ('tst_cs_2', 'ent_forge', 'tst_ct_1', 'tst_col_ok', 'recording');
  insert into forge.collection_sessions
    (id, enterprise_id, task_id, collector_id, status)
  values ('tst_cs_3', 'ent_forge', 'tst_ct_1', 'tst_col_ok', 'scheduled');
  raise notice 'FAIL: 同一采集员出现两场并行采集';
exception
  when unique_violation then raise notice 'PASS: 并行采集被唯一索引拒绝';
  when others then raise notice 'FAIL: 错误类型不对 -> %', sqlerrm;
end $$;

\echo ''
\echo '=== 场景 12 · 供应商侧看不到别家的发票与付款 ==='
select set_config('forge.supplier_id', 'sup_a2', true) is not null as s;
select
  (select count(*) from forge.invoices)  as invoices_visible,
  (select count(*) from forge.payments)  as payments_visible,
  (select count(*) from forge.collectors) as collectors_visible;

select set_config('forge.supplier_id', '', true) is not null as s;
select
  (select count(*) from forge.invoices)  as invoices_internal,
  (select count(*) from forge.collectors) as collectors_internal;

\echo ''
\echo '=== 场景 13 · 应付账龄视图 ==='
select settlement_id, payable_outstanding, days_overdue, aging_bucket
  from (select settlement_id, outstanding as payable_outstanding, days_overdue, aging_bucket
          from forge.v_payables_aging) x
 order by settlement_id;

\echo ''
\echo '=== 全部场景执行完毕，回滚 ==='
rollback;
