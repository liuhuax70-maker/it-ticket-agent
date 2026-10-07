output "namespace" {
  description = "业务与依赖所在命名空间"
  value       = module.k8s.namespace
}

output "service_endpoints" {
  description = "集群内服务地址（供 configs/env 与业务服务 ConfigMap 使用）"
  value = {
    postgres   = var.enable_postgres ? "rag-postgres-postgresql.${module.k8s.namespace}.svc.cluster.local:5432" : null
    redis      = var.enable_redis ? "rag-redis-master.${module.k8s.namespace}.svc.cluster.local:6379" : null
    milvus     = var.enable_milvus ? "rag-milvus.${module.k8s.namespace}.svc.cluster.local:19530" : null
    opensearch = var.enable_opensearch ? "rag-opensearch.${module.k8s.namespace}.svc.cluster.local:9200" : null
    kafka      = var.enable_kafka ? "rag-kafka.${module.k8s.namespace}.svc.cluster.local:9092" : null
    keycloak   = var.enable_keycloak ? "rag-keycloak.${module.k8s.namespace}.svc.cluster.local:8080" : null
    opa        = var.enable_opa ? "rag-opa.${module.k8s.namespace}.svc.cluster.local:8181" : null
  }
}
