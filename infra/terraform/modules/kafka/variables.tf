variable "namespace" { type = string }
variable "release_name" { type = string }
variable "chart_version" { type = string }
variable "labels" {
  type    = map(string)
  default = {}
}
variable "brokers" {
  type    = number
  default = 3
}
variable "partitions" {
  type    = number
  default = 6
}
variable "storage_size" {
  type    = string
  default = "20Gi"
}
