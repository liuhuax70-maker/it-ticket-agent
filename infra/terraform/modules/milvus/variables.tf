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
