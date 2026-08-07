# Single image serving every pipeline stage.
#
# Lambda's zip packaging caps combined code + layers at 250MB unzipped. Our
# dependency set is ~820MB (xgboost alone is 417MB), and even the optimizer -
# which loads no model - reaches 249MB from scipy/pandas/numpy alone. Container
# images raise the ceiling to 10GB, so all stages ship as one image and each
# function selects its entrypoint via CMD. One build, one push, six functions
# that cannot drift apart.
FROM public.ecr.aws/lambda/python:3.11

# Build toolchain is needed for any sdist fallback, then removed to keep the
# image lean.
RUN yum install -y gcc gcc-c++ && yum clean all

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Application code. shared/ is the single source of feature engineering and DB
# access for every stage, including training.
COPY shared/     ${LAMBDA_TASK_ROOT}/shared/
COPY lambdas/    ${LAMBDA_TASK_ROOT}/lambdas/
COPY training/   ${LAMBDA_TASK_ROOT}/training/
COPY db_migrations/ ${LAMBDA_TASK_ROOT}/db_migrations/

# Overridden per function by image_config.command in Terraform.
CMD ["lambdas.ingest.handler.lambda_handler"]
