# ============================================================
# Root Terraform Variables
# IBM Cloud OpenShift ROKS Cluster + Transit Gateway
# ============================================================


# ============================================================
# IBM Cloud Variables
# ============================================================

variable "ibmcloud_api_key" {
  description = "IBM Cloud API Key"
  type        = string
  sensitive   = true
}

variable "ibmcloud_cluster_region" {
  description = "IBM Cloud region for cluster resources"
  type        = string
  default     = "ca-tor"
}

variable "ibmcloud_resource_group" {
  description = "IBM Cloud Resource Group name (leave empty to use account default)"
  type        = string
  default     = "default"
}

# ============================================================
# Feature Flags
# ============================================================

variable "create_roks_cluster" {
  description = "Create OpenShift ROKS cluster"
  type        = bool
  default     = true
}

variable "create_roks_transit_gateway" {
  description = "Create Transit Gateway and VPC connections"
  type        = bool
  default     = true
}

variable "create_roks_registry_cos_instance" {
  description = "Create Cloud Object Storage instance for OpenShift registry"
  type        = bool
  default     = true
}

# ============================================================
# Cluster Variables
# ============================================================

variable "roks_cluster_vpc_name" {
  description = "Name of the cluster VPC"
  type        = string
  default     = "tf-cluster-vpc"
}

variable "openshift_cluster_name" {
  description = "Name of the OpenShift cluster (must be ≤32 characters, start with a letter, and contain only letters, numbers, '-', '_', or '.')"
  type        = string
  default     = "tf-openshift-cluster"

  validation {
    condition     = length(var.openshift_cluster_name) <= 32
    error_message = "openshift_cluster_name must be 32 characters or fewer (IBM Cloud limit)."
  }

  validation {
    condition     = can(regex("^[a-zA-Z]", var.openshift_cluster_name))
    error_message = "openshift_cluster_name must begin with a letter."
  }

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9._-]*$", var.openshift_cluster_name))
    error_message = "openshift_cluster_name may only contain letters, numbers, '-', '_', and '.'."
  }
}

variable "openshift_cluster_version" {
  description = "OpenShift major.minor version to deploy (e.g. \"4.18\"). The latest available patch within that major.minor is selected automatically. Leave empty to use the latest available version overall."
  type        = string
  default     = "4.18"
}

variable "roks_workers_per_zone" {
  description = "Number of worker nodes per zone"
  type        = number
  default     = 1
}

variable "roks_min_worker_vcpu_count" {
  description = "Minimum vCPU count for worker nodes (used for auto-selecting flavor)"
  type        = number
  default     = 16
}

variable "roks_min_worker_memory_gb" {
  description = "Minimum memory in GB for worker nodes (used for auto-selecting flavor)"
  type        = number
  default     = 64
}

variable "roks_cos_instance_name" {
  description = "Name of the COS instance for OpenShift registry (defaults to cluster_name-cos)"
  type        = string
  default     = "tf-openshift-cos-instance"
}

# ============================================================
# Transit Gateway Variables
# ============================================================

variable "roks_transit_gateway_name" {
  description = "Name of the Transit Gateway"
  type        = string
  default     = "tf-tgw"
}
