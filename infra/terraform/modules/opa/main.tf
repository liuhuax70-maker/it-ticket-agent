# OPA：策略决策服务。
# 官方没有稳定的 Helm chart，因此直接用 K8s 原生资源部署，
# 策略内容从 services/authz/policies 读取——**线上策略与仓库策略必须同源**，
# 否则改了仓库里的 rag.rego 但线上没生效，是权限事故的典型起因。

locals {
  policy_files = fileset(var.policy_path, "*.rego")
}

resource "kubernetes_config_map" "policies" {
  metadata {
    name      = "rag-opa-policies"
    namespace = var.namespace
    labels    = var.labels
  }

  data = {
    for file in local.policy_files : file => file("${var.policy_path}/${file}")
  }
}

resource "kubernetes_deployment" "this" {
  metadata {
    name      = "rag-opa"
    namespace = var.namespace
    labels    = merge(var.labels, { app = "rag-opa" })
  }

  spec {
    replicas = var.replicas

    selector {
      match_labels = { app = "rag-opa" }
    }

    template {
      metadata {
        labels = merge(var.labels, { app = "rag-opa" })
        annotations = {
          # 策略变更时自动滚动重启（配置变了但进程不重启，是另一个常见坑）
          "checksum/policies" = sha256(join("", [for f in local.policy_files : filesha256("${var.policy_path}/${f}")]))
        }
      }

      container {
        name  = "opa"
        image = var.image
        args  = ["run", "--server", "--log-level=info", "/policies"]

        port {
          name           = "http"
          container_port = 8181
        }

        resources {
          requests = { cpu = "50m", memory = "128Mi" }
          limits   = { cpu = "500m", memory = "512Mi" }
        }

        liveness_probe {
          http_get {
            path = "/health"
            port = 8181
          }
          initial_delay_seconds = 5
        }

        readiness_probe {
          http_get {
            path = "/health?bundles=true"
            port = 8181
          }
          initial_delay_seconds = 3
        }

        volume_mount {
          name       = "policies"
          mount_path = "/policies"
          read_only  = true
        }
      }

      volume {
        name = "policies"

        config_map {
          name = kubernetes_config_map.policies.metadata[0].name
        }
      }
    }
  }
}

resource "kubernetes_service" "this" {
  metadata {
    name      = "rag-opa"
    namespace = var.namespace
    labels    = merge(var.labels, { app = "rag-opa" })
  }

  spec {
    selector = { app = "rag-opa" }

    port {
      name        = "http"
      port        = 8181
      target_port = 8181
    }
  }
}
