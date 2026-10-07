variable "namespace" {
  type = string
}

variable "labels" {
  type    = map(string)
  default = {}
}

variable "quota_cpu" {
  type    = string
  default = "32"
}

variable "quota_memory" {
  type    = string
  default = "64Gi"
}

variable "quota_cpu_limit" {
  type    = string
  default = "64"
}

variable "quota_memory_limit" {
  type    = string
  default = "128Gi"
}
