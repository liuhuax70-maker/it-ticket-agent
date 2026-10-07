# 命名空间与基础配额：所有组件的公共前置。
# 单独成模块的原因：配额与 LimitRange 是**平台约束**，
# 不应该散落在各个组件的 chart values 里，否则换环境时容易漏配。

resource "kubernetes_namespace" "this" {
  metadata {
    name   = var.namespace
    labels = var.labels
  }
}

# 单容器默认上限：防止某个组件忘记声明 requests/limits 后被打满节点
resource "kubernetes_limit_range" "this" {
  metadata {
    name      = "rag-defaults"
    namespace = kubernetes_namespace.this.metadata[0].name
  }

  spec {
    limit {
      type            = "Container"
      default_request = { cpu = "200m", memory = "512Mi" }
      default         = { cpu = "1", memory = "2Gi" }
    }
  }
}

# 命名空间总配额：本地/预发集群多团队共用时的兜底保护
resource "kubernetes_resource_quota" "this" {
  metadata {
    name      = "rag-quota"
    namespace = kubernetes_namespace.this.metadata[0].name
  }

  spec {
    hard = {
      "requests.cpu"    = var.quota_cpu
      "requests.memory" = var.quota_memory
      "limits.cpu"      = var.quota_cpu_limit
      "limits.memory"   = var.quota_memory_limit
    }
  }
}
