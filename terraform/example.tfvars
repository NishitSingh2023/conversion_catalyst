# us-west-2 (Oregon): the hackathon lab account is only enabled in this region.
# A wrong-region call surfaces as "access denied", not "wrong region".
aws_region           = "us-west-2"
environment          = "dev"
project_name         = "lead-assignment"
db_instance_class    = "db.t3.micro"
# 20 GB does not survive the real dataset: ~0.5 GB of per-run intermediates
# accumulate nightly with nothing pruning them, plus 3.1 GB of transient sort
# space during eligibility. See variables.tf for the measured breakdown.
db_allocated_storage = 100
lsq_api_base_url     = "https://mock-lsq.local/api"
