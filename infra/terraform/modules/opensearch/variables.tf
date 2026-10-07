variable "namespace" { type = string }
variable "release_name" { type = string }
variable "chart_version" { type = string }
variable "storage_class" { type = string }
variable "storage_size" { type = string }
variable "labels" {
  type    = map(string)
  default = {}
}
variable "wait" {
  type    = bool
  default = true
}
variable "replicas" {
  type    = number
  default = 1
}
variable "security_enabled" {
  description = "是否启用 OpenSearch 安全插件（生产必须为 true，并配置证书）"
  type        = bool
  default     = false
}
