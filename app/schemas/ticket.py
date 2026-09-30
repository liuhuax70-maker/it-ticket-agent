"""工单相关数据模型与枚举。

枚举取值与 `开发流程/05-接口与数据契约设计.md` §2.1 保持一致，
并要求在 API、数据库、编排 State 三处统一使用。
"""

from enum import Enum

from pydantic import BaseModel, Field


class Intent(str, Enum):
    """意图识别结果（MVP：二分）。"""

    CONSULT = "consult"
    SENSITIVE = "sensitive"


class Source(str, Enum):
    """工单提交来源。"""

    EMPLOYEE = "employee"
    CUSTOMER = "customer"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class SendStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class TicketStatus(str, Enum):
    PROCESSING = "processing"
    AWAITING_REVIEW = "awaiting_review"
    SENT = "sent"
    REJECTED = "rejected"
    FAILED = "failed"


class ReviewDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class Message(BaseModel):
    """对话消息（多轮上下文）。"""

    role: str
    content: str
    created_at: str | None = None


class TicketQueryRequest(BaseModel):
    """POST /ticket/query 请求体。"""

    ticket_id: str = Field(description="工单标识")
    session_id: str = Field(description="会话标识，多轮需一致")
    query: str = Field(min_length=1, max_length=4000, description="问题正文")
    source: Source = Source.EMPLOYEE
    history: list[Message] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    """POST /ticket/review 请求体。"""

    ticket_id: str
    session_id: str
    decision: ReviewDecision
    comment: str | None = None
    edited_reply: str | None = Field(default=None, description="人工修改后的最终文案，优先使用")


class TicketInfo(BaseModel):
    """工单状态视图（会话查询返回）。"""

    ticket_id: str
    query: str
    intent: Intent | None = None
    status: TicketStatus | None = None
    draft: str | None = None
    reply: str | None = None
    citations: list[str] = Field(default_factory=list)
    review_status: ReviewStatus | None = None
    created_at: str | None = None
    updated_at: str | None = None
