# Kafka（KRaft 模式，无需 ZooKeeper）：raw-documents / chunk-events / feedback-events
resource "helm_release" "this" {
  name       = var.release_name
  namespace  = var.namespace
  repository = "https://charts.bitnami.com/bitnami"
  chart      = "kafka"
  version    = var.chart_version
  wait       = true

  values = [
    yamlencode({
      kraft          = { enabled = true }
      controller     = { replicaCount = 1 }
      broker         = { replicaCount = var.brokers }
      zookeeper      = { enabled = false }
      listeners      = { client = { protocol = "PLAINTEXT" } }
      provisioning = {
        enabled = true
        topics = [
          { name = "raw-documents", partitions = var.partitions, replicationFactor = 1 },
          { name = "chunk-events", partitions = var.partitions, replicationFactor = 1 },
          { name = "feedback-events", partitions = var.partitions, replicationFactor = 1 },
        ]
      }
      persistence = { enabled = true, size = var.storage_size }
    })
  ]
}
