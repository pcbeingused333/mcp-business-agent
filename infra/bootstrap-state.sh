#!/usr/bin/env bash
#
# Create the S3 bucket that holds Terraform's state.
#
# Not managed by Terraform, and it cannot be: a configuration cannot create the
# bucket its own backend already needs to exist in order to run. This is the one
# resource that is bootstrapped by hand, which is why it is a script and not a
# paragraph in the README telling you to click through the console.
#
# Run once per account. Safe to re-run.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="mcp-business-agent-tfstate-${ACCOUNT}"

if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "Bucket ${BUCKET} already exists."
else
  echo "Creating ${BUCKET} in ${REGION}"
  # us-east-1 is the one region that rejects a LocationConstraint.
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=${REGION}"
  fi
fi

# State is the record of what exists. A truncated write or a bad apply is
# recoverable from a previous version and is not recoverable without one.
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled

# State holds resource attributes in clear text. Nothing here is a credential,
# but that is a property of today's configuration, not a guarantee about
# tomorrow's.
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

# Old state versions are tiny but unbounded; expire them so this does not become
# the one line item on the bill that grows forever.
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --lifecycle-configuration '{
    "Rules": [{
      "ID": "expire-noncurrent-state",
      "Status": "Enabled",
      "Filter": {},
      "NoncurrentVersionExpiration": {"NoncurrentDays": 90}
    }]
  }'

echo
echo "Bucket ready: ${BUCKET}"
echo "Now run:  terraform init -migrate-state"
