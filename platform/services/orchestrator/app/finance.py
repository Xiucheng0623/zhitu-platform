"""
FORGE Orchestrator · 财务域路由（发票 / 付款 / 核销 / 三单匹配）
================================================================
移除 ERPNext 之后，这一层就是平台唯一的应付侧真相来源。

几条写进代码而不是写进文档的规矩：
  1. 金额只用 Decimal 运算，绝不经过 float。
  2. 发票金额自洽（total = amount + tax）由库 CHECK 兜底，这里先算好再写。
  3. 「能付多少」永远以 v_settlement_match 的计算为准，不信任前端传来的数字。
  4. 核销超付、并发采集这类约束在库内拦截；这里只负责把数据库的
     check_violation 翻译成 422，而不是自己再判一遍（两处判断必然漂移）。
  5. 每次状态流转都写审计，与业务写入同事务。
"""
from __future__ import annotations

import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import db
from .fsm import check_invoice_transition, check_payment_transition
from .netutil import client_ip
from .security import Principal, require

router = APIRouter(prefix="/api/v1")

TWO = Decimal("0.01")


def _money(v: Any) -> str:
    if v is None:
        return "0.00"
    return str(Decimal(str(v)).quantize(TWO, rounding=ROUND_HALF_UP))


def _ts(v: Any) -> Optional[str]:
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else v


def _row(r: Dict[str, Any], money=(), ts=()) -> Dict[str, Any]:
    d = dict(r)
    for k in money:
        if k in d:
            d[k] = _money(d[k])
    for k in ts:
        if k in d:
            d[k] = _ts(d[k])
    for k, v in list(d.items()):
        if isinstance(v, Decimal):
            d[k] = str(v)
    return d


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _code(prefix: str, ident: str) -> str:
    """单据号：前缀 + 年月 + id 派生短码。用 id 派生而不是流水号，
    是为了避免 count+1 在并发下撞号；唯一索引仍然兜底。"""
    import datetime as _dt
    ym = _dt.datetime.now().strftime("%Y%m")
    return f"{prefix}-{ym}-{ident.split('_')[-1][:6].upper()}"


def _db_error(exc: Exception) -> Optional[HTTPException]:
    """把库里的约束错误翻译成业务可读的 422，而不是抛 500。
    库里拦得住、API 说得清，这才叫约束生效。"""
    msg = str(exc)
    if "核销超付" in msg or "核销金额超过付款单" in msg:
        return HTTPException(422, msg)
    if "尚未确认" in msg:
        return HTTPException(422, msg)
    if "ux_invoices_no_alive" in msg:
        return HTTPException(422, "该供应商已存在同一发票号的票据，疑似重复报销")
    if "ux_payments_voucher" in msg:
        return HTTPException(422, "该银行流水号已登记过，疑似重复付款")
    if "ck_invoice" in msg:
        return HTTPException(422, "发票金额不自洽（价税合计必须等于不含税金额 + 税额）")
    if "ck_payment" in msg:
        return HTTPException(422, "付款单缺少必要凭证（回单号 / 审批人）")
    if "unique" in msg.lower() or "duplicate" in msg.lower():
        return HTTPException(422, "唯一约束冲突")
    return None


# ---------------------------------------------------------------------------
# 发票
# ---------------------------------------------------------------------------


class InvoiceCreate(BaseModel):
    supplier_id: str = Field(min_length=1)
    settlement_id: Optional[str] = None
    invoice_no: str = Field(min_length=1, max_length=64)
    invoice_type: str = Field(default="vat_special",
                              pattern="^(vat_special|vat_normal|e_invoice|overseas)$")
    amount: Decimal = Field(gt=0, description="不含税金额")
    tax_rate: Decimal = Field(default=Decimal("0.06"), ge=0, le=1)
    object_key: Optional[str] = None


class InvoiceReject(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.get("/invoices")
async def list_invoices(status: Optional[str] = None,
                        supplier_id: Optional[str] = None,
                        settlement_id: Optional[str] = None,
                        limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                        p: Principal = Depends(require("invoice:read"))):
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """
            select i.id, i.supplier_id, sup.name as supplier_name, i.settlement_id,
                   i.code, i.invoice_no, i.invoice_type, i.amount, i.tax_rate,
                   i.tax_amount, i.total, i.status, i.issued_at, i.received_at,
                   i.verified_at, i.verified_by, i.reject_reason, i.object_key,
                   i.created_at, i.updated_at
              from forge.invoices i
              join forge.suppliers sup on sup.id = i.supplier_id
             where (%(status)s::text is null or i.status = %(status)s)
               and (%(supplier_id)s::text is null or i.supplier_id = %(supplier_id)s)
               and (%(settlement_id)s::text is null or i.settlement_id = %(settlement_id)s)
             order by i.created_at desc
             limit %(limit)s offset %(offset)s
            """,
            {"status": status, "supplier_id": supplier_id,
             "settlement_id": settlement_id, "limit": limit, "offset": offset},
        )
        rows = [_row(r, money=("amount", "tax_amount", "total", "tax_rate"),
                     ts=("issued_at", "received_at", "verified_at", "created_at", "updated_at"))
                for r in await cur.fetchall()]
    return {"rows": rows, "total": len(rows), "page": offset // limit + 1, "page_size": limit}


@router.post("/invoices", status_code=201)
async def create_invoice(body: InvoiceCreate, request: Request,
                         p: Principal = Depends(require("invoice:write"))):
    tax = (body.amount * body.tax_rate).quantize(TWO, rounding=ROUND_HALF_UP)
    total = body.amount + tax
    inv_id = _new_id("inv")

    async with db.tenant_conn(p) as conn:
        # 挂账的结算单必须属于同一个供应商，否则等于把 A 的票挂到 B 的应付上
        if body.settlement_id:
            cur = await conn.execute(
                "select supplier_id, payable, status from forge.settlements where id = %s",
                (body.settlement_id,),
            )
            st = await cur.fetchone()
            if st is None:
                raise HTTPException(404, "not found")
            if st["supplier_id"] != body.supplier_id:
                raise HTTPException(422, "结算单与供应商不匹配")
            if total > st["payable"]:
                raise HTTPException(
                    422, f"发票金额 {total} 超过该结算单应付 {st['payable']}，请核对")

        try:
            await conn.execute(
                """
                insert into forge.invoices
                       (id, enterprise_id, supplier_id, settlement_id, code, invoice_no,
                        invoice_type, amount, tax_rate, tax_amount, total, status,
                        object_key, created_by)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s, %s)
                """,
                (inv_id, p.tenant_id, body.supplier_id, body.settlement_id,
                 _code("INV", inv_id), body.invoice_no, body.invoice_type,
                 body.amount, body.tax_rate, tax, total, body.object_key, p.sub),
            )
        except Exception as exc:  # noqa: BLE001
            mapped = _db_error(exc)
            if mapped:
                raise mapped
            raise

        await db.write_audit(conn, p, "invoice.create", inv_id,
                             f"{body.invoice_no} total={total}", client_ip(request))
    return {"id": inv_id, "amount": _money(body.amount), "tax_amount": _money(tax),
            "total": _money(total), "status": "draft"}


async def _invoice_step(p: Principal, inv_id: str, target: str, ip: Optional[str],
                        note: str = "", extra_set: str = "", extra_params: tuple = (),
                        action: str = "invoice.step") -> Dict[str, Any]:
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            "select id, status, settlement_id, total from forge.invoices where id = %s",
            (inv_id,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, "not found")
        check_invoice_transition(row["status"], target)

        sets = ["status = %s", "updated_at = now()"]
        params: List[Any] = [target]
        if extra_set:
            sets.append(extra_set)
            params.extend(extra_params)
        params.append(inv_id)
        await conn.execute(
            f"update forge.invoices set {', '.join(sets)} where id = %s", tuple(params))

        # 发票核验通过 → 对应结算单推进到 invoiced（财务闭环，不是两套状态各走各的）
        if target == "verified" and row["settlement_id"]:
            cur = await conn.execute(
                "select status from forge.settlements where id = %s for update",
                (row["settlement_id"],))
            st = await cur.fetchone()
            if st and st["status"] == "confirmed":
                await conn.execute(
                    "update forge.settlements set status = 'invoiced', updated_at = now() where id = %s",
                    (row["settlement_id"],))
                await db.write_audit(conn, p, "settlement.advance", row["settlement_id"],
                                     "confirmed -> invoiced（发票核验通过，自动推进）", ip)

        await db.write_audit(conn, p, action, inv_id,
                             f"{row['status']} -> {target} {note}".strip(), ip)
        return {"ok": True, "status": target, "previous_status": row["status"]}


@router.post("/invoices/{inv_id}/issue")
async def invoice_issue(inv_id: str, request: Request,
                        p: Principal = Depends(require("invoice:write"))):
    return await _invoice_step(p, inv_id, "issued", client_ip(request),
                               extra_set="issued_at = now()", action="invoice.issue")


@router.post("/invoices/{inv_id}/receive")
async def invoice_receive(inv_id: str, request: Request,
                          p: Principal = Depends(require("invoice:write"))):
    return await _invoice_step(p, inv_id, "received", client_ip(request),
                               extra_set="received_at = now()", action="invoice.receive")


@router.post("/invoices/{inv_id}/verify")
async def invoice_verify(inv_id: str, request: Request,
                         p: Principal = Depends(require("invoice:verify"))):
    return await _invoice_step(p, inv_id, "verified", client_ip(request),
                               extra_set="verified_at = now(), verified_by = %s",
                               extra_params=(p.sub,), action="invoice.verify")


@router.post("/invoices/{inv_id}/reject")
async def invoice_reject(inv_id: str, body: InvoiceReject, request: Request,
                         p: Principal = Depends(require("invoice:verify"))):
    return await _invoice_step(p, inv_id, "rejected", client_ip(request),
                               note=body.reason,
                               extra_set="reject_reason = %s", extra_params=(body.reason,),
                               action="invoice.reject")


@router.post("/invoices/{inv_id}/void")
async def invoice_void(inv_id: str, request: Request,
                       p: Principal = Depends(require("invoice:verify"))):
    return await _invoice_step(p, inv_id, "void", client_ip(request),
                               action="invoice.void")


# ---------------------------------------------------------------------------
# 付款
# ---------------------------------------------------------------------------


class Allocation(BaseModel):
    settlement_id: str = Field(min_length=1)
    amount: Decimal = Field(gt=0)


class PaymentCreate(BaseModel):
    supplier_id: str = Field(min_length=1)
    amount: Decimal = Field(gt=0)
    method: str = Field(default="bank_transfer",
                        pattern="^(bank_transfer|alipay|wechat|check|offset)$")
    payee_name: Optional[str] = None
    payee_bank: Optional[str] = None
    payee_account: Optional[str] = None
    note: str = ""
    allocations: List[Allocation] = Field(default_factory=list)


class PaymentSettle(BaseModel):
    voucher_no: str = Field(min_length=1, max_length=64)


class PaymentFail(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.get("/payments")
async def list_payments(status: Optional[str] = None,
                        supplier_id: Optional[str] = None,
                        limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                        p: Principal = Depends(require("payment:read"))):
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """
            select pm.id, pm.supplier_id, sup.name as supplier_name, pm.code, pm.amount,
                   pm.method, pm.voucher_no, pm.status, pm.requested_by, pm.approved_by,
                   pm.paid_at, pm.created_at,
                   coalesce((select sum(a.amount) from forge.payment_allocations a
                              where a.payment_id = pm.id), 0) as allocated
              from forge.payments pm
              join forge.suppliers sup on sup.id = pm.supplier_id
             where (%(status)s::text is null or pm.status = %(status)s)
               and (%(supplier_id)s::text is null or pm.supplier_id = %(supplier_id)s)
             order by pm.created_at desc
             limit %(limit)s offset %(offset)s
            """,
            {"status": status, "supplier_id": supplier_id,
             "limit": limit, "offset": offset},
        )
        rows = [_row(r, money=("amount", "allocated"), ts=("paid_at", "created_at"))
                for r in await cur.fetchall()]
    return {"rows": rows, "total": len(rows), "page": offset // limit + 1, "page_size": limit}


@router.post("/payments", status_code=201)
async def create_payment(body: PaymentCreate, request: Request,
                         p: Principal = Depends(require("payment:write"))):
    total_alloc = sum((a.amount for a in body.allocations), Decimal("0"))
    if total_alloc > body.amount:
        raise HTTPException(422, f"核销合计 {total_alloc} 超过付款金额 {body.amount}")

    pay_id = _new_id("pay")
    async with db.tenant_conn(p) as conn:
        try:
            await conn.execute(
                """
                insert into forge.payments
                       (id, enterprise_id, supplier_id, code, amount, method, payee_name,
                        payee_bank, payee_account, status, requested_by, note)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)
                """,
                (pay_id, p.tenant_id, body.supplier_id, _code("PAY", pay_id),
                 body.amount, body.method, body.payee_name, body.payee_bank,
                 body.payee_account, p.sub, body.note),
            )
            for a in body.allocations:
                await conn.execute(
                    """
                    insert into forge.payment_allocations
                           (enterprise_id, payment_id, settlement_id, amount, created_by)
                    values (%s, %s, %s, %s, %s)
                    """,
                    (p.tenant_id, pay_id, a.settlement_id, a.amount, p.sub),
                )
        except Exception as exc:  # noqa: BLE001
            mapped = _db_error(exc)
            if mapped:
                raise mapped
            raise

        await db.write_audit(conn, p, "payment.create", pay_id,
                             f"amount={body.amount} allocs={len(body.allocations)}",
                             client_ip(request))
    return {"id": pay_id, "amount": _money(body.amount), "status": "pending",
            "allocated": _money(total_alloc)}


async def _payment_step(p: Principal, pay_id: str, target: str, ip: Optional[str],
                        extra_set: str = "", extra_params: tuple = (),
                        action: str = "payment.step", detail: str = "") -> Dict[str, Any]:
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            "select id, status, amount from forge.payments where id = %s", (pay_id,))
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, "not found")
        check_payment_transition(row["status"], target)

        sets = ["status = %s", "updated_at = now()"]
        params: List[Any] = [target]
        if extra_set:
            sets.append(extra_set)
            params.extend(extra_params)
        params.append(pay_id)
        try:
            await conn.execute(
                f"update forge.payments set {', '.join(sets)} where id = %s", tuple(params))
        except Exception as exc:  # noqa: BLE001
            mapped = _db_error(exc)
            if mapped:
                raise mapped
            raise

        await db.write_audit(conn, p, action, pay_id,
                             f"{row['status']} -> {target} {detail}".strip(), ip)
        return {"ok": True, "status": target, "previous_status": row["status"]}


@router.post("/payments/{pay_id}/approve")
async def payment_approve(pay_id: str, request: Request,
                          p: Principal = Depends(require("payment:approve"))):
    return await _payment_step(p, pay_id, "approved", client_ip(request),
                               extra_set="approved_by = %s, approved_at = now()",
                               extra_params=(p.sub,), action="payment.approve")


@router.post("/payments/{pay_id}/settle")
async def payment_settle(pay_id: str, body: PaymentSettle, request: Request,
                         p: Principal = Depends(require("payment:approve"))):
    """登记到账：必须带回单号。没有回单号的"已付款"就是账上的一笔悬案。"""
    return await _payment_step(p, pay_id, "succeeded", client_ip(request),
                               extra_set="voucher_no = %s, paid_at = now()",
                               extra_params=(body.voucher_no,),
                               action="payment.settle", detail=body.voucher_no)


@router.post("/payments/{pay_id}/fail")
async def payment_fail(pay_id: str, body: PaymentFail, request: Request,
                       p: Principal = Depends(require("payment:approve"))):
    return await _payment_step(p, pay_id, "failed", client_ip(request),
                               extra_set="fail_reason = %s", extra_params=(body.reason,),
                               action="payment.fail", detail=body.reason)


@router.post("/payments/{pay_id}/reverse")
async def payment_reverse(pay_id: str, request: Request,
                          p: Principal = Depends(require("payment:approve"))):
    """冲销：已成功付款被撤回。库里的触发器会把对应结算单退回 invoiced，
    不会出现「款退回了但结算单还说已付」的对账灾难。"""
    return await _payment_step(p, pay_id, "reversed", client_ip(request),
                               action="payment.reverse")


# ---------------------------------------------------------------------------
# 三单匹配 / 账龄 / 汇总
# ---------------------------------------------------------------------------
@router.get("/finance/match")
async def three_way_match(match_state: Optional[str] = None,
                          period: Optional[str] = None,
                          p: Principal = Depends(require("settlement:read"))):
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """
            select settlement_id, supplier_id, supplier_name, code, period,
                   accepted_items, amount, deduction, payable,
                   invoice_count, invoiced_amount, verified_amount,
                   allocated_amount, paid_amount, outstanding,
                   accepted_task_count, accepted_item_count, match_state
              from forge.v_settlement_match
             where (%(state)s::text is null or match_state = %(state)s)
               and (%(period)s::text is null or period = %(period)s)
             order by period desc, code
            """,
            {"state": match_state, "period": period},
        )
        money = ("amount", "deduction", "payable", "invoiced_amount", "verified_amount",
                 "allocated_amount", "paid_amount", "outstanding")
        rows = [_row(r, money=money) for r in await cur.fetchall()]
    return {"rows": rows, "total": len(rows)}


@router.get("/finance/aging")
async def payables_aging(p: Principal = Depends(require("settlement:read"))):
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """
            select settlement_id, supplier_id, supplier_name, code, period,
                   outstanding, due_at, days_overdue, aging_bucket
              from forge.v_payables_aging
             order by days_overdue desc, settlement_id
            """
        )
        rows = [_row(r, money=("outstanding",), ts=("due_at",)) for r in await cur.fetchall()]
    return {"rows": rows, "total": len(rows)}


@router.get("/finance/summary")
async def finance_summary(p: Principal = Depends(require("settlement:read"))):
    async with db.tenant_conn(p) as conn:
        cur = await conn.execute(
            """
            select
              coalesce(sum(payable), 0)                                   as payable_total,
              coalesce(sum(paid_amount), 0)                               as paid_total,
              coalesce(sum(outstanding), 0)                               as outstanding_total,
              coalesce(sum(outstanding) filter (where match_state = 'amount_mismatch'), 0)
                                                                          as mismatched_total,
              count(*) filter (where match_state = 'missing_invoice')      as missing_invoice,
              count(*) filter (where match_state = 'amount_mismatch')      as amount_mismatch,
              count(*) filter (where match_state = 'awaiting_verification') as awaiting_verify,
              count(*) filter (where match_state = 'ready_to_pay')         as ready_to_pay,
              count(*) filter (where match_state = 'partial_paid')         as partial_paid,
              count(*) filter (where match_state = 'settled')              as settled
              from forge.v_settlement_match
            """
        )
        s = _row(await cur.fetchone(),
                 money=("payable_total", "paid_total", "outstanding_total", "mismatched_total"))
        cur = await conn.execute(
            """
            select
              count(*) filter (where status = 'pending')   as payments_pending,
              count(*) filter (where status = 'approved')  as payments_approved,
              coalesce(sum(amount) filter (where status in ('pending','approved')), 0)
                                                           as payments_inflight
              from forge.payments
            """
        )
        pay = _row(await cur.fetchone(), money=("payments_inflight",))
        cur = await conn.execute(
            """
            select count(*) filter (where status = 'verified') as invoices_verified,
                   count(*) filter (where status = 'received') as invoices_to_verify,
                   count(*) filter (where status = 'rejected') as invoices_rejected,
                   count(*) filter (where status = 'void')     as invoices_void,
                   coalesce(sum(total) filter (where status = 'received'), 0)
                                                               as invoices_pending_amount
              from forge.invoices
            """
        )
        inv = _row(await cur.fetchone(), money=("invoices_pending_amount",))
    return {"settlements": s, "payments": pay, "invoices": inv}
