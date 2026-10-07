# Keycloak：签发租户/部门声明的 JWT（claim 映射见 infra/docker/keycloak/realm-rag.json）
resource "helm_release" "this" {
  name       = var.release_name
  namespace  = var.namespace
  repository = "https://codecentric.github.io/helm-charts"
  chart      = "keycloakx"
  version    = var.chart_version
  wait       = true

  values = [
    yamlencode({
      replicas = var.replicas
      # 生产必须用 externalDatabase（复用 Postgres），此处仅体现参数位置
      db = var.use_external_database ? {
        vendor   = "postgres"
        host     = var.database_host
        database = var.database_name
        username = var.database_username
        } : {
        vendor = "dev-file"
      }
      http = { relativePath = "/" }
      resources = {
        requests = { cpu = "250m", memory = "1Gi" }
        limits   = { cpu = "1", memory = "2Gi" }
      }
      # realm 导入同样可用 ConfigMap 挂载，保持与本地 docker 环境同源
      extraInitContainers = var.realm_import ? ["/opt/keycloak/bin/kc.sh import --file /realm/realm-rag.json || true"] : []
    })
  ]
}
