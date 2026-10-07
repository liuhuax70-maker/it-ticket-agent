# staging 环境：与 prod 同构（同样的组件与开关），只是容量小一档。
# 存在意义是「用同一套配置验证变更」——staging 与 prod 结构不一致的团队，
# 最终都会在生产上做第一次真实测试。
module "rag" {
  source = "../../"

  environment = "staging"
  namespace   = "rag-staging"

  enable_postgres   = true
  enable_redis      = true
  enable_milvus     = true
  enable_opensearch = true
  enable_kafka      = true
  enable_keycloak   = true
  enable_opa        = true

  storage_sizes = {
    postgres   = "30Gi"
    milvus     = "100Gi"
    opensearch = "50Gi"
  }
}
