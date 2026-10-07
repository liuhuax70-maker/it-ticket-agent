# Milvus：向量检索。standalone 模式自带 etcd + MinIO 依赖，
# 生产建议改为 cluster 模式并外置对象存储（values 里 cluster.enabled = true）。
resource "helm_release" "this" {
  name       = var.release_name
  namespace  = var.namespace
  repository = "https://zilliztech.github.io/milvus-helm"
  chart      = "milvus"
  version    = var.chart_version
  wait       = var.wait
  timeout    = 900

  values = [
    yamlencode({
      cluster = { enabled = false }
      etcd = {
        replicaCount = 1
        persistence  = { enabled = true, storageClass = var.storage_class, size = "10Gi" }
      }
      minio = {
        mode        = "standalone"
        persistence = { enabled = true, storageClass = var.storage_class, size = var.storage_size }
      }
      standalone = {
        resources = {
          requests = { cpu = "500m", memory = "2Gi" }
          limits   = { cpu = "2", memory = "4Gi" }
        }
        persistence = { enabled = true, storageClass = var.storage_class }
      }
    })
  ]
}
