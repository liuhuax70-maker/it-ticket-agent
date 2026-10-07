variable "namespace" { type = string }
variable "release_name" { type = string }
variable "chart_version" { type = string }
variable "labels" {
  type    = map(string)
  default = {}
}
variable "auth_enabled" {
  description = "是否启用密码认证（生产必须为 true）"
  type        = bool
  default     = true
}
variable "password" {
  type      = string
  sensitive = true
  default   = ""
}
variable "persistence_enabled" {
  type    = bool
  default = false
}
variable "storage_size" {
  type    = string
  default = "8Gi"
}
