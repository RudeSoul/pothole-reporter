FROM public.ecr.aws/lambda/python:3.12@sha256:ab6df78b68b50723c93741bb7f9ea9f68c7cf3433359b33e4f37db5266567f9c
ENV POTHOLE_LAMBDA_BASE_IMAGE_DIGEST=sha256:ab6df78b68b50723c93741bb7f9ea9f68c7cf3433359b33e4f37db5266567f9c

COPY service/requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt
RUN python -m pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Build from infra/aws-yolo and place an evaluated release bundle in artifacts/.
# The build intentionally fails when any release-bundle file is absent.
COPY artifacts/model.onnx /opt/model/model.onnx
COPY artifacts/model-manifest.json /opt/model/model-manifest.json
COPY artifacts/runtime-parity.json /opt/model/runtime-parity.json
COPY service/handler.py service/detector.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.lambda_handler"]
