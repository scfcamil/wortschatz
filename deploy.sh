#!/usr/bin/env bash
# Build the push image, push it to ECR and apply the Terraform. Re-run after any code change.
# Needs: aws CLI (logged in), terraform >= 1.6, docker with buildx.
set -euo pipefail
cd "$(dirname "$0")"

REGION="${AWS_REGION:-eu-south-1}"
TAG="$(date +%Y%m%d%H%M%S)"
TF=(terraform -chdir=terraform)

"${TF[@]}" init -input=false
# 1. the ECR repo has to exist before an image can be pushed
"${TF[@]}" apply -input=false -auto-approve -var "region=$REGION" -target=aws_ecr_repository.push
REPO="$("${TF[@]}" output -raw ecr_repository_url)"

# 2. build for arm64 (native on Apple Silicon); --provenance=false keeps a manifest Lambda accepts
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${REPO%%/*}"
docker buildx build --platform linux/arm64 --provenance=false -t "$REPO:$TAG" --push push/

# 3. everything else
"${TF[@]}" apply -input=false -var "region=$REGION" -var "push_image_tag=$TAG"

echo
echo "API URL:    $("${TF[@]}" output -raw api_url)"
echo "API secret: terraform -chdir=terraform output -raw api_secret"
