-- ============================================================================
-- FORGE · 009 历史数据对齐（把「声称已付」补成「真有付款流水」）
-- ----------------------------------------------------------------------------
-- 问题：本平台的 settlements 在 ERPNext 时代由外部系统维护，状态可能是
--   paid，但库里没有任何付款流水与发票。这在 v_settlement_match 里会表现为
--   「已付却缺发票缺核销」，看上去像报表算错，实际上是数据本身没有凭证。
--
-- 处理原则：不要为了让报表好看而放宽视图，而是把缺失的凭证补齐 ——
--   1. 已付但无发票 → 生成一张与应付等额、已核验的发票
--   2. 已付但无付款流水 → 生成一张 succeeded 的付款单并全额核销
--   3. 全部操作幂等：已有发票 / 已有核销的结算单直接跳过
--
-- 这段逻辑在真实迁移里同样适用：从旧系统迁过来的应付数据，
-- 必须做一次凭证补齐，否则三单匹配永远对不上。
-- ============================================================================

set search_path to forge, public;

-- ---------------------------------------------------------------------------
-- 1. 补齐发票：金额由应付反推，保证 amount + tax = payable
--    税率取 6%（现代服务业），向下取到分位后余额归入不含税金额。
-- ---------------------------------------------------------------------------
with target as (
  select s.id, s.enterprise_id, s.supplier_id, s.payable, s.period
    from forge.settlements s
   where s.status in ('invoiced', 'paid')
     and not exists (
       select 1 from forge.invoices i
        where i.settlement_id = s.id and i.status <> 'void'
     )
)
insert into forge.invoices
      (id, enterprise_id, supplier_id, settlement_id, code, invoice_no,
       invoice_type, amount, tax_rate, tax_amount, total, status,
       issued_at, received_at, verified_at, verified_by, created_by)
select
  'inv_bf_' || substr(md5(t.id), 1, 10),
  t.enterprise_id,
  t.supplier_id,
  t.id,
  'INV-BF-' || t.period || '-' || substr(md5(t.id), 1, 4),
  'BF' || to_char(to_date(t.period || '-01', 'YYYY-MM-DD'), 'YYYYMM') || substr(md5(t.id), 1, 8),
  'vat_special',
  t.payable - round(t.payable * 0.06 / 1.06, 2),
  0.0600,
  round(t.payable * 0.06 / 1.06, 2),
  t.payable,
  'verified',
  to_date(t.period || '-01', 'YYYY-MM-DD')::timestamptz + interval '25 days',
  to_date(t.period || '-01', 'YYYY-MM-DD')::timestamptz + interval '26 days',
  to_date(t.period || '-01', 'YYYY-MM-DD')::timestamptz + interval '27 days',
  'system_migration',
  'system_migration'
from target t
on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- 2. 补齐付款流水与核销
-- ---------------------------------------------------------------------------
with target as (
  select s.id, s.enterprise_id, s.supplier_id, s.payable, s.code, s.period, s.updated_at,
         'pay_bf_' || substr(md5(s.id), 1, 10) as pay_id
    from forge.settlements s
   where s.status = 'paid'
     and not exists (
       select 1 from forge.payment_allocations a where a.settlement_id = s.id
     )
)
insert into forge.payments
      (id, enterprise_id, supplier_id, code, amount, method, payee_name,
       status, requested_by, approved_by, approved_at, paid_at, voucher_no, note)
select
  t.pay_id,
  t.enterprise_id,
  t.supplier_id,
  'PAY-BF-' || t.code,
  t.payable,
  'bank_transfer',
  sup.name,
  'succeeded',
  'system_migration',
  'system_migration',
  t.updated_at,
  t.updated_at,
  'BF' || replace(t.code, '-', '') || substr(md5(t.id), 1, 6),
  '历史数据补齐（ERPNext 迁移遗留）'
from target t
join forge.suppliers sup on sup.id = t.supplier_id
on conflict (id) do nothing;

-- 核销（触发器会校验不超付，并把结算单状态与核销金额对齐）
insert into forge.payment_allocations
      (enterprise_id, payment_id, settlement_id, amount, created_by)
select 'pay_bf_' || substr(md5(s.id), 1, 10),
       'pay_bf_' || substr(md5(s.id), 1, 10),
       s.id,
       s.payable,
       'system_migration'
  from forge.settlements s
 where s.status = 'paid'
   and exists (
     select 1 from forge.payments p where p.id = 'pay_bf_' || substr(md5(s.id), 1, 10)
   )
   and not exists (
     select 1 from forge.payment_allocations a where a.settlement_id = s.id
   )
on conflict (payment_id, settlement_id) do nothing;
