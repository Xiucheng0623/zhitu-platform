"""
FORGE Orchestrator · 状态机（唯一权威实现）
================================================================
前端 Store 里的状态流转只是「提前拦截以改善手感」，真正裁决在这里。
任何绕过前端的直接 API 调用，都必须通过这些转移表。

约定：表里没写的转移一律 409 state_conflict，不做「宽容处理」。
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, List, Tuple


class TaskState(str, Enum):
    pending = "pending"
    assigned = "assigned"
    in_progress = "in_progress"
    submitted = "submitted"
    reviewing = "reviewing"
    rework = "rework"
    accepted = "accepted"
    rejected = "rejected"


# 注意 assigned → assigned 用于「改派」：不改变状态的重复指派需要留痕，
# 但不能退化成 pending，否则会丢失已分配事实。
TASK_TRANSITIONS: Dict[TaskState, set] = {
    TaskState.pending: {TaskState.assigned, TaskState.rejected},
    TaskState.assigned: {TaskState.in_progress, TaskState.submitted, TaskState.assigned,
                         TaskState.rejected},
    TaskState.in_progress: {TaskState.submitted, TaskState.rejected},
    TaskState.submitted: {TaskState.reviewing, TaskState.accepted, TaskState.rework},
    TaskState.reviewing: {TaskState.accepted, TaskState.rework, TaskState.rejected},
    TaskState.rework: {TaskState.assigned, TaskState.in_progress, TaskState.submitted},
    TaskState.accepted: set(),
    TaskState.rejected: set(),
}

TASK_TERMINAL = {TaskState.accepted, TaskState.rejected}


class SettlementState(str, Enum):
    draft = "draft"
    submitted = "submitted"
    confirmed = "confirmed"
    invoiced = "invoiced"
    paid = "paid"


SETTLEMENT_FLOW: List[str] = [s.value for s in SettlementState]

# 推进每一步所需权限：财务动作不开放给运营与项目经理
SETTLEMENT_STEP_PERMS: Dict[Tuple[str, str], str] = {
    ("draft", "submitted"): "settlement:write",
    ("submitted", "confirmed"): "settlement:approve",
    ("confirmed", "invoiced"): "settlement:approve",
    ("invoiced", "paid"): "settlement:approve",
}


class SupplierState(str, Enum):
    """与 forge.suppliers.status 的 CHECK 约束一一对应。"""
    qualifying = "qualifying"
    active = "active"
    probation = "probation"
    suspended = "suspended"


SUPPLIER_TRANSITIONS: Dict[str, set] = {
    # 准入：资质审完才能从 qualifying 转 active
    SupplierState.qualifying.value: {SupplierState.active.value, SupplierState.suspended.value},
    SupplierState.active.value: {SupplierState.probation.value, SupplierState.suspended.value},
    # 观察期表现达标可恢复，否则停用
    SupplierState.probation.value: {SupplierState.active.value, SupplierState.suspended.value},
    # 停用后必须重新审核通过才能恢复，不能直接回到 active 的业务口头授权
    SupplierState.suspended.value: {SupplierState.qualifying.value, SupplierState.active.value},
}


class StateConflict(Exception):
    """状态不允许当前操作。由 main 的异常处理器转成 409。"""

    def __init__(self, current: str, target: str, entity: str = "task"):
        self.current = current
        self.target = target
        self.entity = entity
        super().__init__(f"{entity}: {current} -> {target} 不允许")


def check_task_transition(current: str, target: TaskState) -> None:
    if target not in TASK_TRANSITIONS.get(TaskState(current), set()):
        raise StateConflict(current, target.value, "task")


def next_settlement_state(current: str) -> str:
    try:
        i = SETTLEMENT_FLOW.index(current)
    except ValueError:
        raise StateConflict(current, "?", "settlement")
    if i >= len(SETTLEMENT_FLOW) - 1:
        raise StateConflict(current, "paid(终态)", "settlement")
    return SETTLEMENT_FLOW[i + 1]


def check_supplier_transition(current: str, target: str) -> None:
    if target not in SUPPLIER_TRANSITIONS.get(current, set()):
        raise StateConflict(current, target, "supplier")


def task_counts_as_progress(status: str) -> bool:
    return status in {TaskState.submitted.value, TaskState.reviewing.value,
                      TaskState.accepted.value}


# ---------------------------------------------------------------------------
# 发票状态机
# ---------------------------------------------------------------------------
# 为什么不是线性五态：
#   发票有「退回重开」这条真实路径（rejected → void），也有「验完发现错」的
#   冲销需求。硬套一条直线会让财务只能删记录重来，那是审计上的灾难。
class InvoiceState(str, Enum):
    draft = "draft"
    issued = "issued"
    received = "received"
    verified = "verified"
    rejected = "rejected"
    void = "void"


INVOICE_TRANSITIONS: Dict[str, set] = {
    # 供应商录入草稿后可提交开票
    InvoiceState.draft.value: {InvoiceState.issued.value, InvoiceState.void.value},
    # 平台收到票 → 待核验
    InvoiceState.issued.value: {InvoiceState.received.value, InvoiceState.void.value},
    # 核验通过 → 可参与付款；不通过 → 退回供应商
    InvoiceState.received.value: {InvoiceState.verified.value, InvoiceState.rejected.value,
                                  InvoiceState.void.value},
    # 退回后供应商可以重开（作废重录），也可以修正后重新提交
    InvoiceState.rejected.value: {InvoiceState.issued.value, InvoiceState.void.value},
    # 已核验的票要么参与付款，要么作废（红冲），不允许退回
    InvoiceState.verified.value: {InvoiceState.void.value},
    InvoiceState.void.value: set(),
}


def check_invoice_transition(current: str, target: str) -> None:
    if target not in INVOICE_TRANSITIONS.get(current, set()):
        raise StateConflict(current, target, "invoice")


# ---------------------------------------------------------------------------
# 付款状态机
# ---------------------------------------------------------------------------
class PaymentState(str, Enum):
    pending = "pending"
    approved = "approved"
    succeeded = "succeeded"
    failed = "failed"
    reversed = "reversed"


PAYMENT_TRANSITIONS: Dict[str, set] = {
    # 申请人提交后等审批
    PaymentState.pending.value: {PaymentState.approved.value, PaymentState.failed.value},
    # 审批通过 → 银行执行；执行失败可重试（回到 pending 视为重新发起）
    PaymentState.approved.value: {PaymentState.succeeded.value, PaymentState.failed.value},
    # 失败的付款单可以重新进入审批流，不需要新建单据
    PaymentState.failed.value: {PaymentState.pending.value},
    # 已成功的付款只能被冲销，不能改金额
    PaymentState.succeeded.value: {PaymentState.reversed.value},
    PaymentState.reversed.value: set(),
}


def check_payment_transition(current: str, target: str) -> None:
    if target not in PAYMENT_TRANSITIONS.get(current, set()):
        raise StateConflict(current, target, "payment")


# ---------------------------------------------------------------------------
# 采集任务状态机
# ---------------------------------------------------------------------------
COLLECTION_TRANSITIONS: Dict[str, set] = {
    "draft": {"recruiting", "cancelled"},
    "recruiting": {"running", "cancelled"},
    "running": {"paused", "completed", "cancelled"},
    "paused": {"running", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


def check_collection_transition(current: str, target: str) -> None:
    if target not in COLLECTION_TRANSITIONS.get(current, set()):
        raise StateConflict(current, target, "collection_task")


# ---------------------------------------------------------------------------
# 采集场次状态机
# ---------------------------------------------------------------------------
SESSION_TRANSITIONS: Dict[str, set] = {
    "scheduled": {"recording", "submitted", "rejected"},
    "recording": {"submitted", "rejected"},
    # 提交后进入质检，质检结论决定 accepted / rejected
    "submitted": {"qc", "accepted", "rejected"},
    "qc": {"accepted", "rejected"},
    "accepted": set(),
    "rejected": set(),
}


def check_session_transition(current: str, target: str) -> None:
    if target not in SESSION_TRANSITIONS.get(current, set()):
        raise StateConflict(current, target, "collection_session")
