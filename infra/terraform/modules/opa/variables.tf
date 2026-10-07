variable "namespace" { type = string }
variable "labels" {
  type    = map(string)
  default = {}
}
variable "policy_path" {
  description = "策略目录（应与 services/authz/policies 同源）"
  type        = string
}
variable "image" {
  type    = string
  default = "openpolicyagent/opa:0.68.0"
}
variable "replicas" {
  type    = number
  default = 2
}
