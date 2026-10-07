# Redis：查询缓存、限流计数、embedding 缓存。
# 注意：本组件是**可降级依赖**（缓存未命中只是变慢），因此 dev 关闭持久化以省资源。
resource "helm_release" "this" {
  name       = var.release_name
  namespace  = var.namespace
  repository = "https://charts.bitnami.com/bitnami"
  chart      = "redis"
  version    = var.chart_version
  wait       = true

  values = [
    yamlencode({
      architecture = "standalone"
      auth         = { enabled = var.auth_enabled, password = var.password }
      master = {
        persistence = { enabled = var.persistence_enabled, size = var.storage_size }
        resources = {
          requests = { cpu = "100m", memory = "256Mi" }
          limits   = { cpu = "1", memory = "1Gi" }
        }
      }
      metrics = { enabled = true }
    })
  ]
}
