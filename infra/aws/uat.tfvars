// UAT stack — apply with:
//   tofu workspace select uat && tofu apply -var-file=uat.tfvars
// terraform.tfvars is auto-loaded first; everything here overrides it.

project    = "grain-uat"
api_domain = "api.uat.grain.natejly.com"
web_origin = "https://uat.grain.natejly.com"

acm_certificate_arn = "arn:aws:acm:us-east-1:518060119468:certificate/54221df8-657d-4077-a276-f86d03130d93"

// Same free-tier sizing as prod (see terraform.tfvars).

// Reuse the account-wide GitHub OIDC provider created by the prod stack —
// IAM permits one provider per URL per account.
enable_github_oidc       = true
github_repo              = "natejly/grain"
github_oidc_provider_arn = "arn:aws:iam::518060119468:oidc-provider/token.actions.githubusercontent.com"
