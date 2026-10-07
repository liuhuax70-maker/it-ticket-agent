variable "environment" {
  description = "环境名（dev / staging / prod），用于命名与打标签"
  type        = string
}

variable "namespace" {
  description = "部署命名空间"
  type        = string
  default     = "rag"
}

variable "kubeconfig_path" {
  description = "kubeconfig 路径"
  type        = string
  default     = "~/.kube/config"
}

variable "kube_context" {
  description = "kubeconfig context（留空使用当前上下文）"
  type        = string
  default     = null
}

# ---------------------------------------------------------------- 组件开关

variable "enable_postgres" {
  description = "部署 Postgres（元数据与 ACL）"
  type        = bool
  default     = true
}

variable "enable_redis" {
  description = "部署 Redis（缓存与限流计数）"
  type        = bool
  default     = true
}

variable "enable_milvus" {
  description = "部署 Milvus（向量库）"
  type        = bool
  default     = true
}

variable "enable_opensearch" {
  description = "部署 OpenSearch（BM25 全文检索）"
  type        = bool
  default     = true
}

variable "enable_kafka" {
  description = "部署 Kafka（异步接入通道）"
  type        = bool
  default     = false
}

variable "enable_keycloak" {
  description = "部署 Keycloak（身份）"
  type        = bool
  default     = false
}

variable "enable_opa" {
  description = "部署 OPA（策略决策）"
  type        = bool
  default     = false
}

# ---------------------------------------------------------------- 资源规格

variable "chart_versions" {
  description = "各依赖的 Helm chart 版本，按环境固定，避免漂移"
  type = object({
    postgresql = string
    redis      = string
    milvus     = string
    opensearch = string
    kafka      = string
    keycloak   = string
  })
  default = {
    postgresql = "15.5.38"
    redis      = "20.6.1"
    milvus     = "4.2.6"
    opensearch = "2.25.0"
    kafka      = "30.1.8"
    keycloak   = "5.0.0"
  }
}

variable "storage_class" {
  description = "持久化使用的 StorageClass"
  type        = string
  default     = "standard"
}

variable "storage_sizes" {
  description = "各组件持久化容量"
  type = object({
    postgres   = string
    milvus     = string
    opensearch = string
  })
  default = {
    postgres   = "20Gi"
    milvus     = "50Gi"
    opensearch = "50Gi"
  }
}

variable "postgres_password" {
  description = "Postgres 应用用户口令（敏感；请用 TF_VAR_postgres_password 注入，勿写入 tfvars）"
  type        = string
  sensitive   = true
  default     = ""
}
