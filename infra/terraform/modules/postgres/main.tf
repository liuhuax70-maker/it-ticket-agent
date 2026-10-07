# Postgres：文档台账、分块明细、ACL、反馈（迁移由 Alembic 负责，见 scripts/migrate.py）
resource "helm_release" "this" {
  name       = var.release_name
  namespace  = var.namespace
  repository = "https://charts.bitnami.com/bitnami"
  chart      = "postgresql"
  version    = var.chart_version
  wait       = var.wait

  values = [
    yamlencode({
      auth = {
        username         = var.username
        database         = var.database
        password         = var.postgres_password
        postgresPassword = var.postgres_password
      }
      primary = {
        persistence = {
          enabled      = true
          storageClass = var.storage_class
          size         = var.storage_size
        }
        resources = {
          requests = { cpu = "500m", memory = "1Gi" }
          limits   = { cpu = "2", memory = "4Gi" }
        }
      }
      metrics = { enabled = true }
    })
  ]
}
