# The anki wheel needs glibc >= 2.35, newer than the AWS Lambda Python base image (AL2023, 2.34),
# so the image is built on Debian bookworm with the AWS Lambda runtime interface client.
FROM public.ecr.aws/docker/library/python:3.13-slim-bookworm

ENV HOME=/tmp PYTHONUNBUFFERED=1
WORKDIR /var/task
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY handler.py .

ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["handler.handler"]
