# =============================================================
# permission-aware-rag 基础设施编排（K8s 依赖组件）
#
# 分工：
#   * Terraform 管**集群内依赖组件**（PG/Redis/Milvus/OpenSearch/Kafka/Keycloak/OPA），
#     它们是第三方软件，用 Helm chart 部署并固定版本；
#   * 业务服务（api-gateway / query-orchestrator / ...）用 infra/k8s 的
#     Kustomize + Helm 部署，走 CI 发布流水线，不在 Terraform 里管——
#     否则每改一行服务配置都要走 state 变更。
# =============================================================

locals {
  common_labels = {
    "app.kubernetes.io/part-of"    = "permission-aware-rag"
    "app.kubernetes.io/managed-by" = "terraform"
    "rag.env"                      = var.environment
  }
}

module "k8s" {
  source = "./modules/k8s"

  namespace = var.namespace
  labels    = local.common_labels
}

module "postgres" {
  source = "./modules/postgres"
  count  = var.enable_postgres ? 1 : 0

  namespace       = module.k8s.namespace
  release_name    = "rag-postgres"
  chart_version   = var.chart_versions.postgresql
  storage_class   = var.storage_class
  storage_size    = var.storage_sizes.postgres
  postgres_password = var.postgres_password
  labels          = local.common_labels
}

module "redis" {
  source = "./modules/redis"
  count  = var.enable_redis ? 1 : 0

  namespace     = module.k8s.namespace
  release_name  = "rag-redis"
  chart_version = var.chart_versions.redis
  labels        = local.common_labels
}

module "milvus" {
  source = "./modules/milvus"
  count  = var.enable_milvus ? 1 : 0

  namespace     = module.k8s.namespace
  release_name  = "rag-milvus"
  chart_version = var.chart_versions.milvus
  storage_class = var.storage_class
  storage_size  = var.storage_sizes.milvus
  labels        = local.common_labels
}

module "opensearch" {
  source = "./modules/opensearch"
  count  = var.enable_opensearch ? 1 : 0

  namespace     = module.k8s.namespace
  release_name  = "rag-opensearch"
  chart_version = var.chart_versions.opensearch
  storage_class = var.storage_class
  storage_size  = var.storage_sizes.opensearch
  labels        = local.common_labels
}

module "kafka" {
  source = "./modules/kafka"
  count  = var.enable_kafka ? 1 : 0

  namespace     = module.k8s.namespace
  release_name  = "rag-kafka"
  chart_version = var.chart_versions.kafka
  labels        = local.common_labels
}

module "keycloak" {
  source = "./modules/keycloak"
  count  = var.enable_keycloak ? 1 : 0

  namespace     = module.k8s.namespace
  release_name  = "rag-keycloak"
  chart_version = var.chart_versions.keycloak
  labels        = local.common_labels
}

module "opa" {
  source = "./modules/opa"
  count  = var.enable_opa ? 1 : 0

  namespace = module.k8s.namespace
  labels    = local.common_labels
  # 与 services/authz/policies 同源，避免线上策略与仓库策略不一致
  policy_path = "${path.module}/../../services/authz/policies"
}
