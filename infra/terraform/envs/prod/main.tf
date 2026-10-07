# prod 环境：全组件开启，容量放大；口令通过 TF_VAR_postgres_password 注入，不落盘。
# 用法：
#   cd infra/terraform/envs/prod
#   export TF_VAR_postgres_password=...
#   terraform init && terraform plan

module "rag" {
  source = "../../"

  environment = "prod"
  namespace   = "rag-prod"

  enable_postgres   = true
  enable_redis      = true
  enable_milvus     = true
  enable_opensearch = true
  enable_kafka      = true
  enable_keycloak   = true
  enable_opa        = true

  storage_sizes = {
    postgres   = "100Gi"
    milvus     = "500Gi"
    opensearch = "200Gi"
  }

  # 依赖组件版本在此显式钉住，升级走 PR 评审
  chart_versions = {
    postgresql = "15.5.38"
    redis      = "20.6.1"
    milvus     = "4.2.6"
    opensearch = "2.25.0"
    kafka      = "30.1.8"
    keycloak   = "5.0.0"
  }
}
