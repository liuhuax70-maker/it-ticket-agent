package rag

# =============================================================
# RAG 平台访问策略
#
# 输入契约（由 api-gateway 构造，见 apps/api-gateway/app/middleware/identity.py）：
#   input.action          动作名，如 "chat" / "documents:write"
#   input.user.tenant_id  调用方租户（来自 Keycloak 的 tenant_id 声明）
#   input.user.roles      角色列表
#   input.resource.type   资源类型（当前固定 "rag"）
#
# 输出：allow 布尔 + reason 字符串（便于审计与排障）
#
# 设计取向：**默认拒绝**。策略里只白名单放行，新增动作必须显式登记，
# 避免「新加个接口忘了配策略」变成默认开放。
# =============================================================

default allow = false

# 只读动作：普通用户即可
read_actions := {"chat", "documents:read", "feedback:write"}

# 写动作：需要 rag_writer 或管理员
write_actions := {"documents:write", "documents:delete"}

is_admin {
    "rag_admin" = input.user.roles[_]
}

is_writer {
    "rag_writer" = input.user.roles[_]
}

valid_tenant {
    tenant := input.user.tenant_id
    tenant != ""
    tenant != null
}

# 管理员：租户有效即可
allow {
    is_admin
    valid_tenant
}

# 只读：租户有效 + 动作在白名单
allow {
    valid_tenant
    read_actions[input.action]
}

# 写入：租户有效 + 动作在白名单 + 具备写角色
allow {
    valid_tenant
    write_actions[input.action]
    is_writer
}

reason = "admin" {
    is_admin
    valid_tenant
} else = "read_allowed" {
    valid_tenant
    read_actions[input.action]
} else = "write_allowed" {
    valid_tenant
    write_actions[input.action]
    is_writer
} else = "tenant_missing" {
    not valid_tenant
} else = "role_missing" {
    valid_tenant
    write_actions[input.action]
    not is_writer
} else = "action_not_permitted" {
    valid_tenant
} else = "denied"
