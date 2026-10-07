terraform {
  required_version = ">= 1.6.0"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.31"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.14"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # 远程 state（团队协作必须启用；本地验证时保持注释）
  # backend "s3" {
  #   bucket         = "rag-terraform-state"
  #   key            = "permission-aware-rag/terraform.tfstate"
  #   region         = "ap-northeast-1"
  #   dynamodb_table = "rag-terraform-lock"
  #   encrypt        = true
  # }
}

provider "kubernetes" {
  config_path    = var.kubeconfig_path
  config_context = var.kube_context
}

provider "helm" {
  kubernetes {
    config_path    = var.kubeconfig_path
    config_context = var.kube_context
  }
}
