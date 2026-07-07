"""
regions.py — region lists shared by cleanup_routes.py and instances_routes.py.
Matches the standard AWS/Azure/GCP default (opt-in-excluded) regions.
"""

ALL_AWS_REGIONS = [
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "ap-south-1", "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ap-southeast-1", "ap-southeast-2",
    "ca-central-1",
    "eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3", "eu-north-1",
    "sa-east-1",
]

ALL_AZURE_REGIONS = [
    "eastus", "eastus2", "centralus", "northcentralus", "southcentralus",
    "westus", "westus2", "westus3", "westcentralus",
    "canadacentral", "canadaeast",
    "brazilsouth", "brazilsoutheast",
    "northeurope", "westeurope", "uksouth", "ukwest",
    "francecentral", "francesouth",
    "germanywestcentral", "germanynorth",
    "norwayeast", "swedencentral",
    "switzerlandnorth", "switzerlandwest",
    "italynorth", "polandcentral", "spaincentral",
    "eastasia", "southeastasia", "japaneast", "japanwest",
    "koreacentral", "koreasouth",
    "australiaeast", "australiasoutheast", "australiacentral",
    "centralindia", "southindia", "westindia",
    "uaenorth", "uaecentral", "qatarcentral",
    "southafricanorth",
    "mexicocentral",
]

ALL_GCP_REGIONS = [
    "us-central1", "us-east1", "us-east4", "us-east5",
    "us-south1", "us-west1", "us-west2", "us-west3", "us-west4",
    "northamerica-northeast1", "northamerica-northeast2",
    "southamerica-east1", "southamerica-west1",
    "europe-west1", "europe-west2", "europe-west3", "europe-west4",
    "europe-west6", "europe-west8", "europe-west9", "europe-west10", "europe-west12",
    "europe-north1", "europe-central2", "europe-southwest1",
    "asia-east1", "asia-east2",
    "asia-northeast1", "asia-northeast2", "asia-northeast3",
    "asia-south1", "asia-south2",
    "asia-southeast1", "asia-southeast2",
    "australia-southeast1", "australia-southeast2",
    "me-west1", "me-central1", "me-central2",
    "africa-south1",
]
