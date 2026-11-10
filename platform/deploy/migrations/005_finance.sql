-- ============================================================================
-- FORGE · 005 财务域（发票 / 付款 / 核销 / 三单匹配）
-- ----------------------------------------------------------------------------
-- 为什么必须有这个文件：
--   移除 ERPNext 之后，原本由 ERP 承载的「Purchase Invoice」「Payment Entry」
--   「三单匹配」不再有任何外部系统兜底。这不是可以留空的装饰表 ——
--   没有它，settlements 走到 invoiced / paid 就是一句空话，
--   钱有没有付、付了多少、发票对不对得上，系统里没有任何记录。
--
-- 设计要点（会计域的真实约束，不是拍脑袋）：
--   1. 金额一律 numeric(14,2)；发票价税合计必须等于不含税金额 + 税额，
--      用 CHECK 约束强制，不允许应用层"算一下就好"。
--   2. 同一供应商的发票号唯一（排除已作废），防止重复报销。
--      这是财务上最常见的舞弊路径，必须在库里堵死，不能靠 UI 校验。
--   3. 付款与结算单是多对多：一笔款可分次付多张结算单，一张结算单可分多次付。
--      核销表 payment_allocations 是这条关系的唯一事实来源。
--   4. 累计核销不得超过应付，用触发器在库内拦截（超付是不可逆的会计事故）。
--   5. 结算单的 paid 状态由核销金额自动推导，不靠人工点按钮 ——
--      否则必然出现"状态说已付、流水说没付"的对账灾难。
-- ============================================================================

set search_path to forge, public;

-- ---------------------------------------------------------------------------
-- 1. 发票
-- ---------------------------------------------------------------------------
create table if not exists forge.invoices (
  id              text primary key,
  enterprise_id   text not null references forge.enterprises(id) on delete restrict,
  supplier_id     text not null references forge.suppliers(id) on delete restrict,
  -- 一份结算单原则上对应一张发票，但允许先挂账后补票，故可空
  settlement_id   text references forge.settlements(id) on delete set null,
  code            text not null,
  invoice_no      text not null,
  invoice_type    text not null default 'vat_special'
                  check (invoice_type in ('vat_special','vat_normal','e_invoice','overseas')),
  -- 不含税金额
  amount          numeric(14,2) not null check (amount >= 0),
  tax_rate        numeric(5,4) not null default 0.0600
                  check (tax_rate >= 0 and tax_rate <= 1),
  tax_amount      numeric(14,2) not null default 0 check (tax_amount >= 0),
  -- 价税合计
  total           numeric(14,2) not null check (total >= 0),
  status          text not null default 'draft'
                  check (status in ('draft','issued','received','verified','rejected','void')),
  issued_at       timestamptz,
  received_at     timestamptz,
  verified_at     timestamptz,
  verified_by     text,
  reject_reason   text,
  -- MinIO 上的 PDF / OFD 原件
  object_key      text,
  created_by      text not null,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  constraint ck_invoice_total check (total = amount + tax_amount),
  -- 已核验的发票必须留下核验人与时间，否则无法追溯谁放的行
  constraint ck_invoice_verified_has_actor
    check (status <> 'verified' or (verified_by is not null and verified_at is not null))
);

-- 税额容差 ±0.01：真发票是按行舍入后汇总的，强行要求分毫不差会把
-- 合法票据挡在门外。容差是会计现实，不是放水。
alter table forge.invoices drop constraint if exists ck_invoice_tax_consistency;
alter table forge.invoices add constraint ck_invoice_tax_consistency
  check (abs(tax_amount - round(amount * tax_rate, 2)) <= 0.01);
create unique index if not exists ux_invoices_code
  on forge.invoices (enterprise_id, code);
-- 同一供应商同一发票号只能出现一次；作废票不占用号码，允许重开
create unique index if not exists ux_invoices_no_alive
  on forge.invoices (enterprise_id, supplier_id, invoice_no)
  where status <> 'void';
create index if not exists ix_invoices_tenant_status
  on forge.invoices (enterprise_id, status);
create index if not exists ix_invoices_settlement
  on forge.invoices (settlement_id);

-- ---------------------------------------------------------------------------
-- 2. 付款单（一次付款指令 / 一笔银行流水）
-- ---------------------------------------------------------------------------
create table if not exists forge.payments (
  id              text primary key,
  enterprise_id   text not null references forge.enterprises(id) on delete restrict,
  supplier_id     text not null references forge.suppliers(id) on delete restrict,
  code            text not null,
  amount          numeric(14,2) not null check (amount > 0),
  method          text not null default 'bank_transfer'
                  check (method in ('bank_transfer','alipay','wechat','check','offset')),
  payee_name      text,
  payee_bank      text,
  payee_account   text,
  -- 银行回单号 / 支付平台流水号
  voucher_no      text,
  status          text not null default 'pending'
                  check (status in ('pending','approved','succeeded','failed','reversed')),
  requested_by    text not null,
  approved_by     text,
  approved_at     timestamptz,
  paid_at         timestamptz,
  fail_reason     text,
  note            text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  constraint ck_payment_approved_has_actor
    check (status in ('pending','failed') or (approved_by is not null and approved_at is not null)),
  -- 付款成功必须有回单号与到账时间，否则无法与银行流水对账
  constraint ck_payment_succeeded_evidence
    check (status <> 'succeeded' or (voucher_no is not null and paid_at is not null))
);
create unique index if not exists ux_payments_code
  on forge.payments (enterprise_id, code);
-- 同一笔银行流水不能被登记两次（重复付款的典型症状）
create unique index if not exists ux_payments_voucher
  on forge.payments (enterprise_id, voucher_no)
  where voucher_no is not null and status <> 'reversed';
create index if not exists ix_payments_tenant_status
  on forge.payments (enterprise_id, status);
create index if not exists ix_payments_supplier
  on forge.payments (supplier_id, paid_at desc);

-- ---------------------------------------------------------------------------
-- 3. 付款核销（付款单 ↔ 结算单，多对多）
-- ---------------------------------------------------------------------------
create table if not exists forge.payment_allocations (
  id              bigserial primary key,
  enterprise_id   text not null,
  payment_id      text not null references forge.payments(id) on delete cascade,
  settlement_id   text not null references forge.settlements(id) on delete restrict,
  amount          numeric(14,2) not null check (amount > 0),
  created_at      timestamptz not null default now(),
  created_by      text not null
);
create unique index if not exists ux_alloc_payment_settlement
  on forge.payment_allocations (payment_id, settlement_id);
create index if not exists ix_alloc_settlement
  on forge.payment_allocations (settlement_id);

-- ---------------------------------------------------------------------------
-- 4. 触发器：核销不得超付；结算单 paid 状态由核销推导
-- ---------------------------------------------------------------------------
create or replace function forge.guard_allocation() returns trigger as $$
declare
  v_payable   numeric(14,2);
  v_already   numeric(14,2);
  v_pay_total numeric(14,2);
  v_allocated numeric(14,2);
  v_status    text;
begin
  select payable, status into v_payable, v_status
    from forge.settlements where id = new.settlement_id;
  if v_payable is null then
    raise exception '结算单 % 不存在', new.settlement_id;
  end if;
  -- 还没确认的对账单金额随时可能被改，此时核销等于给未定稿的数字付款
  if v_status in ('draft', 'submitted') then
    raise exception '结算单 % 尚未确认（%），不可核销', new.settlement_id, v_status
      using errcode = 'check_violation';
  end if;

  -- 已核销金额（排除本次，支持更新场景）。
  -- 只统计「还有效」的付款：失败或已冲销的付款单，其核销行不再占用应付款，
  -- 否则冲销之后这笔应付就永久锁死、再也付不出去。
  select coalesce(sum(a.amount), 0) into v_already
    from forge.payment_allocations a
    join forge.payments pm on pm.id = a.payment_id
   where a.settlement_id = new.settlement_id
     and pm.status not in ('failed', 'reversed')
     and (tg_op <> 'INSERT' or a.id <> new.id);

  if v_already + new.amount > v_payable + 0.005 then
    raise exception '核销超付：应付 %，已核销 %，本次 %',
      v_payable, v_already, new.amount
      using errcode = 'check_violation';
  end if;

  -- 付款单总额也要够，不能拿 1000 的付款单核销 5000
  select amount into v_pay_total from forge.payments where id = new.payment_id;
  select coalesce(sum(amount), 0) into v_allocated
    from forge.payment_allocations
   where payment_id = new.payment_id
     and (tg_op <> 'INSERT' or id <> new.id);
  if v_allocated + new.amount > v_pay_total + 0.005 then
    raise exception '核销金额超过付款单金额：付款单 %，已分配 %，本次 %',
      v_pay_total, v_allocated, new.amount
      using errcode = 'check_violation';
  end if;

  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_alloc_guard on forge.payment_allocations;
create trigger trg_alloc_guard before insert or update on forge.payment_allocations
  for each row execute function forge.guard_allocation();

-- 已付款金额变化 → 推导结算单是否已付清。
-- 只做 invoiced ↔ paid 之间的双向切换，不越过状态机把 draft 直接变 paid。
create or replace function forge.sync_settlement_paid(p_settlement_id text)
returns void as $$
declare
  v_payable numeric(14,2);
  v_status  text;
  v_paid    numeric(14,2);
begin
  select payable, status into v_payable, v_status
    from forge.settlements where id = p_settlement_id for update;
  if v_payable is null then
    return;
  end if;

  select coalesce(sum(a.amount), 0) into v_paid
    from forge.payment_allocations a
    join forge.payments pm on pm.id = a.payment_id
   where a.settlement_id = p_settlement_id
     and pm.status = 'succeeded';

  if v_status = 'invoiced' and v_paid >= v_payable - 0.005 then
    update forge.settlements set status = 'paid', updated_at = now()
     where id = p_settlement_id;
  elsif v_status = 'paid' and v_paid < v_payable - 0.005 then
    -- 付款被冲销 / 撤销：退回已开票状态，等财务重新处理
    update forge.settlements set status = 'invoiced', updated_at = now()
     where id = p_settlement_id;
  end if;
end;
$$ language plpgsql;

create or replace function forge.trg_sync_settlement_paid() returns trigger as $$
begin
  if tg_op = 'DELETE' then
    perform forge.sync_settlement_paid(old.settlement_id);
    return old;
  end if;
  perform forge.sync_settlement_paid(new.settlement_id);
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_alloc_sync on forge.payment_allocations;
create trigger trg_alloc_sync after insert or update or delete on forge.payment_allocations
  for each row execute function forge.trg_sync_settlement_paid();

-- 付款状态变化（例如 succeeded → reversed）也要重算
create or replace function forge.trg_payment_status_sync() returns trigger as $$
declare r record;
begin
  if new.status is distinct from old.status then
    for r in select distinct settlement_id from forge.payment_allocations
              where payment_id = new.id loop
      perform forge.sync_settlement_paid(r.settlement_id);
    end loop;
  end if;
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_payment_status_sync on forge.payments;
create trigger trg_payment_status_sync after update on forge.payments
  for each row execute function forge.trg_payment_status_sync();

-- ---------------------------------------------------------------------------
-- 5. updated_at 触发器
-- ---------------------------------------------------------------------------
do $$
declare t text;
begin
  foreach t in array array['invoices','payments']
  loop
    execute format('drop trigger if exists trg_touch_%1$s on forge.%1$s', t);
    execute format('create trigger trg_touch_%1$s before update on forge.%1$s
                    for each row execute function forge.touch_updated_at()', t);
  end loop;
end $$;

-- ---------------------------------------------------------------------------
-- 6. 视图：三单匹配（对账单 / 发票 / 付款）
-- ----------------------------------------------------------------------------
-- 这里"三单"落到本平台的真实单据上：
--   对账单 = settlements（数量依据 accepted_items + 已验收任务明细）
--   发票   = invoices
--   付款   = payments 经 payment_allocations 核销后的到账金额
-- 不虚构 ERP 的 PO / GRN，因为本平台没有采购订单这一层。
-- ---------------------------------------------------------------------------
create or replace view forge.v_settlement_match as
with inv as (
  select settlement_id,
         count(*)                                        as invoice_count,
         sum(total)                                      as invoiced_amount,
         sum(total) filter (where status = 'verified')    as verified_amount
    from forge.invoices
   where settlement_id is not null and status <> 'void'
   group by settlement_id
), pay as (
  select a.settlement_id,
         sum(a.amount) filter (where pm.status not in ('failed','reversed'))
                                                            as allocated_amount,
         sum(a.amount) filter (where pm.status = 'succeeded')      as paid_amount
    from forge.payment_allocations a
    join forge.payments pm on pm.id = a.payment_id
   group by a.settlement_id
), evidence as (
  select s.id as settlement_id,
         count(t.id)          as accepted_task_count,
         coalesce(sum(t.done_count), 0) as accepted_item_count
    from forge.settlements s
    left join forge.tasks t
      on t.supplier_id = s.supplier_id
     and t.status = 'accepted'
     and date_trunc('month', t.updated_at) = to_date(s.period || '-01', 'YYYY-MM-DD')
   group by s.id
)
select
  s.id                                as settlement_id,
  s.enterprise_id,
  s.supplier_id,
  sup.name                            as supplier_name,
  s.code,
  s.period,
  s.accepted_items,
  s.amount,
  s.deduction,
  s.payable,
  coalesce(inv.invoice_count, 0)      as invoice_count,
  coalesce(inv.invoiced_amount, 0)    as invoiced_amount,
  coalesce(inv.verified_amount, 0)    as verified_amount,
  coalesce(pay.allocated_amount, 0)   as allocated_amount,
  coalesce(pay.paid_amount, 0)        as paid_amount,
  greatest(s.payable - coalesce(pay.paid_amount, 0), 0) as outstanding,
  coalesce(ev.accepted_task_count, 0) as accepted_task_count,
  coalesce(ev.accepted_item_count, 0) as accepted_item_count,
  case
    when s.status = 'draft'                              then 'not_submitted'
    when coalesce(inv.invoice_count, 0) = 0              then 'missing_invoice'
    when abs(coalesce(inv.invoiced_amount, 0) - s.payable) > 0.01 then 'amount_mismatch'
    when coalesce(inv.verified_amount, 0) < s.payable - 0.01      then 'awaiting_verification'
    when coalesce(pay.paid_amount, 0) >= s.payable - 0.01         then 'settled'
    when coalesce(pay.allocated_amount, 0) > 0                    then 'partial_paid'
    else 'ready_to_pay'
  end                                 as match_state
from forge.settlements s
join forge.suppliers sup on sup.id = s.supplier_id
left join inv on inv.settlement_id = s.id
left join pay on pay.settlement_id = s.id
left join evidence ev on ev.settlement_id = s.id;

-- ---------------------------------------------------------------------------
-- 7. 视图：应付账龄
-- ---------------------------------------------------------------------------
create or replace view forge.v_payables_aging as
select
  s.enterprise_id,
  s.supplier_id,
  sup.name as supplier_name,
  s.id     as settlement_id,
  s.code,
  s.period,
  greatest(s.payable - coalesce(p.paid, 0), 0) as outstanding,
  s.due_at,
  greatest(0, (current_date - s.due_at::date)) as days_overdue,
  case
    when s.due_at is null                      then 'undated'
    when current_date <= s.due_at::date        then 'current'
    when current_date - s.due_at::date <= 30   then 'd1_30'
    when current_date - s.due_at::date <= 60   then 'd31_60'
    when current_date - s.due_at::date <= 90   then 'd61_90'
    else 'd90_plus'
  end as aging_bucket
from forge.settlements s
join forge.suppliers sup on sup.id = s.supplier_id
left join (
  select a.settlement_id, sum(a.amount) as paid
    from forge.payment_allocations a
    join forge.payments pm on pm.id = a.payment_id
   where pm.status = 'succeeded'
   group by a.settlement_id
) p on p.settlement_id = s.id
where s.status <> 'paid';
