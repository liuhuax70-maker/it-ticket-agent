# dev 环境：只要能跑通链路，组件开关按需打开。
# 用法：
#   cd infra/terraform/envs/dev
#   terraform init && terraform plan && terraform apply

module "rag" {
  source = "../../"

  environment = "dev"
  namespace   = "rag-dev"

  enable_postgres   = true
  enable_redis      = true
  enable_milvus     = true
  enable_opensearch = true
  enable_kafka      = false # dev 走同步直连（USE_KAFKA=false）
  enable_keycloak   = true
  enable_opa        = true

  storage_sizes = {
    postgres   = "10Gi"
    milvus     = "20Gi"
    opensearch = "20Gi"
  }
}

output "namespace" {
  value = module.rag.namespace
}

output "endpoints" {
  value = module.rag.service_endpoints
}
