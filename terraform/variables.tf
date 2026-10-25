variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "name" {
  type    = string
  default = "wortschatz"
}

variable "push_image_tag" {
  description = "Tag of the push image in ECR (deploy.sh sets it)"
  type        = string
  default     = "latest"
}

variable "push_schedule" {
  description = "How often pending words are pushed to Anki"
  type        = string
  default     = "rate(7 days)"
}

variable "push_reserved_concurrency" {
  description = "1 = never two runs at once. New AWS accounts with a concurrency quota of 10 must use -1 (see README)"
  type        = number
  default     = 1
}

variable "anki_deck" {
  type    = string
  default = "Wortschatz"
}

variable "english_variant" {
  description = "DeepL target variant for English: EN-GB or EN-US"
  type        = string
  default     = "EN-GB"
}
