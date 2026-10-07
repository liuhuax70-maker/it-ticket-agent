variable "namespace" { type = string }
variable "release_name" { type = string }
variable "chart_version" { type = string }
variable "labels" {
  type    = map(string)
  default = {}
}
variable "replicas" {
  type    = number
  default = 1
}
variable "use_external_database" {
  description = "生产应复用 Postgres（true），dev 用内置文件存储"
  type        = bool
  default     = false
}
variable "database_host" {
  type    = string
  default = ""
}
variable "database_name" {
  type    = string
  default = "keycloak"
}
variable "database_username" {
  type    = string
  default = "rag"
}
variable "realm_import" {
  description = "是否导入仓库内的 realm 定义"
  type        = bool
  default     = true
}
