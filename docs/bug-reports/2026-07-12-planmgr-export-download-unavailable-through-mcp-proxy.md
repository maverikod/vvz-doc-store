# Plan Manager export bytes are unavailable through the MCP Proxy callable surface

## Summary

Plan Manager can successfully create a standard `plan_export` and can open
identity-compression download sessions for the resulting files. The exposed MCP
Proxy callable surface can retrieve the session metadata, but it provides no
operation that can retrieve the file bytes from the downstream authenticated
chunk endpoint.

This is an integration-boundary report. It does not establish whether the
missing capability should be implemented in MCP Proxy, its adapter, or Plan
Manager. Export generation and transfer-session creation are verified working;
only byte retrieval through the available proxy tool surface is blocked.

## Severity and impact

**Impact: blocking for exact export retrieval from an MCP-only client.**

An MCP client can request a Plan Manager export but cannot complete the workflow
by saving the exact exported files. For `doc-store`, this prevents copying the
authoritative export artifacts to the local `docs/plans` directory without
reconstructing or transforming their content. Reconstruction is not an acceptable
workaround because the files must remain byte-identical to Plan Manager output.

No Plan Manager data loss was observed. The completed export job reported
writing both files on the Plan Manager server.

## Affected environment

- Downstream server: `planmgr_1`
- Product: `plan_manager`
- Package version: `0.1.24`
- Adapter declaration: `mcp-proxy-adapter>=8.10.19`
- MCP Proxy general-help catalog: 97 JSON-RPC commands
- Exported plan: `doc-store`

## Prerequisites

1. Access to `planmgr_1` only through the exposed MCP Proxy callable tools.
2. A Plan Manager plan that can be exported.
3. No direct network route or credentials for the downstream Plan Manager HTTP
   service.

## Reproduction

The following sequence was observed live on 2026-07-12. Parameter names below
describe the downstream command calls; authentication material is intentionally
omitted.

1. Invoke downstream `help` with empty parameters through MCP Proxy.
2. Invoke queue-bound `plan_export` for plan `doc-store` and wait for job
   `plan_export_bb2889eb` to complete.
3. Observe the two generated server-side files:

   ```text
   /var/planmgr/export/doc-store/source_spec.md
   /var/planmgr/export/doc-store/spec.yaml
   ```

4. Invoke `transfer_download_begin` for each generated file with identity
   compression. The server creates these sessions:

   ```text
   tr_b28c2a3d52f549b9a478d0800af96a16
   tr_da29e58df46041f3bc4b5cc3bd198617
   ```

5. Invoke `transfer_download_status`. Session metadata is available through
   JSON-RPC.
6. Inspect the live command catalog. There is no JSON-RPC command or exposed MCP
   Proxy tool for reading a download chunk or forwarding an authenticated raw
   request to the downstream endpoint.
7. The transfer contract identifies the byte endpoint as:

   ```text
   GET /api/transfer/downloads/{transfer_id}/chunks?offset=...&limit=...
   ```

8. Attempting direct access from the local workspace is not a viable path. The
   downstream hostname `planmgr` is proxy-internal; a direct `curl` attempt
   failed with exit code 6 (host could not be resolved).

## Actual behavior

- `plan_export` completes successfully.
- `transfer_download_begin` and `transfer_download_status` work through JSON-RPC.
- The actual bytes are available only from an authenticated raw HTTP GET on the
  downstream same origin.
- The available MCP Proxy surface has no raw downstream HTTP forwarding or file
  download operation.
- Consequently, an MCP-only caller cannot retrieve either exported file.

## Expected behavior

After `transfer_download_begin`, an authorized MCP-only caller should be able to
retrieve every byte of the download through the proxy-accessible surface, verify
the declared size and SHA-256 digest, and persist the original bare filenames
without content reconstruction.

The solution must preserve the downstream authorization boundary and must not
require exposing service credentials or proxy-internal hostnames to the client.

## Verified evidence

| Artifact | Size | SHA-256 |
|---|---:|---|
| `source_spec.md` | 16,711 bytes | `a4a832cfc61a0a796a300ed2ebaeb7191593f5a8eb1c2a709b5a9e21974e3910` |
| `spec.yaml` | 25,785 bytes | `d5501c21f53b736f42d249bb10ffe0b9f31508c26a37a9203557686ff97eb92a` |

Additional live evidence:

- Export job `plan_export_bb2889eb` completed.
- The two identity-compression transfer sessions were created successfully.
- MCP Proxy command discovery exposes downstream command invocation, but not an
  authenticated raw downstream chunk-fetch operation.
- The Code Analysis Server cannot bridge this transfer: its upload surface
  accepts raw client-provided PUT chunks into its own upload session and cannot
  consume a Plan Manager transfer ID or remote URL.
- No file was reconstructed. The local `docs/plans` directory remained empty.

## Component-boundary analysis

The demonstrated boundary is:

```text
Plan Manager export creation                 works
Plan Manager transfer session creation       works
Plan Manager transfer metadata over JSON-RPC works
MCP Proxy callable byte retrieval             unavailable
```

The evidence does not prove an internal defect in Plan Manager export creation or
session management. It also does not identify which component must own the fix.
The gap exists in the end-to-end callable contract presented to an MCP-only
client: the contract starts a transfer that the client cannot finish.

## Security constraints

Any fix must:

- preserve authentication and authorization applied to the original proxy
  caller;
- prevent arbitrary URL fetching and arbitrary server-side file reads;
- bind byte retrieval to a transfer session authorized for that caller;
- validate offsets and limits and retain transfer size limits;
- avoid returning downstream credentials, internal hostnames, or filesystem
  paths as a substitute for the bytes;
- retain integrity metadata so the client can verify size and SHA-256.

## Workaround assessment

No acceptable workaround is available through the currently exposed tools.

- Direct HTTP is unavailable because the hostname is internal and the endpoint
  requires downstream authentication.
- Code Analysis Server transfer is a separate upload protocol and cannot pull
  from the Plan Manager session.
- Reconstructing the HRS and MRS from other commands would not guarantee the
  byte-identical standard export and would violate the requirement to copy the
  Plan Manager-produced files unchanged.

## Non-normative implementation options

The following are possible designs, not confirmed root causes or prescribed
solutions:

1. Add an MCP Proxy operation that fetches bounded chunks for an existing,
   caller-authorized downstream transfer session.
2. Add a downstream JSON-RPC `transfer_download_chunk` command and expose it
   through the existing proxy command-call path.
3. Add a constrained proxy download bridge that maps an opaque transfer handle
   to the same-origin chunk endpoint without exposing credentials or arbitrary
   URL access.

The owning teams should select the option that matches the intended transfer and
authorization architecture.

## Fix acceptance criteria

1. An MCP-only client can initiate an export and retrieve both exported files
   without direct access to the downstream host.
2. Retrieval uses only an opaque, caller-authorized transfer identity and bounded
   offset/limit requests.
3. The retrieved `source_spec.md` is exactly 16,711 bytes and verifies as
   `a4a832cfc61a0a796a300ed2ebaeb7191593f5a8eb1c2a709b5a9e21974e3910`
   for the reproduction export.
4. The retrieved `spec.yaml` is exactly 25,785 bytes and verifies as
   `d5501c21f53b736f42d249bb10ffe0b9f31508c26a37a9203557686ff97eb92a`
   for the reproduction export.
5. Invalid, expired, or other-caller transfer identities are rejected.
6. Out-of-range offsets, excessive limits, and unauthorized path or URL inputs
   are rejected.
7. The solution does not expose downstream credentials, internal network
   addresses, or arbitrary file/URL access.
8. Existing `plan_export`, `transfer_download_begin`, and
   `transfer_download_status` behavior remains compatible.
