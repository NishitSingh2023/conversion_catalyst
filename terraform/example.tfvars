# us-west-2 (Oregon): the hackathon lab account is only enabled in this region.
# A wrong-region call surfaces as "access denied", not "wrong region".
aws_region           = "us-west-2"
environment          = "dev"
project_name         = "lead-assignment"
db_instance_class    = "db.t3.micro"
db_allocated_storage = 20
lsq_api_base_url     = "https://mock-lsq.local/api"
