# Lambda container image for the MCP server.
#
# The AWS base image already contains the Runtime Interface Client and the
# Runtime Interface Emulator, so the same image runs unchanged in Lambda and
# locally against LocalStack — no separate dev build to drift out of sync.
FROM public.ecr.aws/lambda/python:3.12

# Dependencies first: this layer is cached whenever only application code
# changes, which is most pushes.
COPY requirements-lambda.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements-lambda.txt

# Only what the server needs at runtime. The agent, the evals, the Streamlit
# demo and the tests are all absent on purpose — smaller image, smaller attack
# surface, faster cold start.
COPY ops/ ${LAMBDA_TASK_ROOT}/ops/
COPY server.py lambda_handler.py ${LAMBDA_TASK_ROOT}/

CMD ["lambda_handler.handler"]
