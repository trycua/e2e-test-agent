# Security Model

This action processes untrusted PR text and application content, and executes
application code inside a remote sandbox. A maintainer-applied label is an
explicit trust decision, not a complete security boundary. Review changes to the
workflow itself before applying the label. Do not enable privileged fork runs or
checkout untrusted code under `pull_request_target` with secrets available.

Use an immutable, reviewed action SHA, least-privilege credentials, a dedicated
Cua pool, and disposable sandboxes. The supported example uses GitHub-hosted
runners, not persistent machines containing unrelated secrets. Do not run
untrusted setup scripts on the runner with production credentials in its
environment. Prompt overrides are trusted configuration, not PR-provided data.

Model phases receive only model credentials and selected runtime configuration;
GitHub and Cua credentials are reserved for the control-plane helpers. Agents
have restricted file tools and no runner-side shell during planning/execution.
Repository source is uploaded without Git credentials. Snapshot filtering is a
best-effort exclusion list, not a secret scanner: review tracked source before
allowing it into the sandbox. Use dummy data and test identities, never production
accounts, in application journeys.

The MCP proxy uses loopback on the runner. Do not expose that port to other hosts
or run unrelated untrusted processes on the same runner. One invocation at a
time per runner is supported; low-level helpers still use fixed local PID files.

Reports, recordings, model messages, and API errors can contain sensitive content.
Only an explicit list of result files is uploaded to GitHub, with limited retention;
raw helper logs remain in runner-local temporary storage. Failed phases include
a bounded stderr excerpt after masking supplied credentials and common token
formats. Redaction is best-effort, not a guarantee that arbitrary application
output is safe to share. Model providers and
Cua infrastructure process the submitted content. Evaluate their access and
retention policies for your repository. Do not automatically publish result
artifacts to public buckets.

Claim deletion and process termination are attempted on failures. Runner loss,
forced cancellation, or API outages can delay cleanup; monitor the pool and use
its lease/lifecycle controls. Never assume cleanup is instantaneous or infallible.

Report vulnerabilities privately using the repository's Security tab and
"Report a vulnerability". Do not disclose credentials or exploitable details
in a public issue.
