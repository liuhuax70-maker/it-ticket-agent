# OpenSearch：BM25 全文检索。
# 中文分词：默认 standard（逐字切分），要提升召回需自行构建带 analysis-ik
# 插件的镜像并在 values 里替换 image，再重建索引。
resource "helm_release" "this" {
  name       = var.release_name
  namespace  = var.namespace
  repository = "https://opensearch-project.github.io/helm-charts"
  chart      = "opensearch"
  version    = var.chart_version
  wait       = var.wait
  timeout    = 900

  values = [
    yamlencode({
      clusterName = var.release_name
      nodeGroup   = "master"
      replicas    = var.replicas
      singleNode  = var.replicas == 1
      persistence = {
        enabled      = true
        storageClass = var.storage_class
        size         = var.storage_size
      }
      resources = {
        requests = { cpu = "500m", memory = "2Gi" }
        limits   = { cpu = "2", memory = "4Gi" }
      }
      opensearchJavaOpts = "-Xms1g -Xmx2g"
      # 生产环境必须启用安全插件并接入证书；此处只体现参数位置
      securityConfig = { enabled = var.security_enabled }
    })
  ]
}
