# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it
privately via
[GitHub's security advisory feature](https://github.com/curlewlabs-com/d1-copy/security/advisories/new).

Do not open a public issue for security vulnerabilities.

## Scope

The tool reads and writes the D1 databases it is named, through Cloudflare's
API, with the token it is given. The security surface is:

- **The token.** It is read from `CLOUDFLARE_API_TOKEN`, sent only as a header
  to `api.cloudflare.com`, and never printed. The pre-signed URLs an export is
  downloaded from and an import is uploaded to never receive it.
- **The data it copies.** Exports and the working databases built from them are
  full copies of the source. They are kept in a directory readable only by the
  user running the tool, removed on every exit the process can observe, and
  removed by the next run if a run was killed. The tool's own messages name
  tables, columns and counts, never row values. An error D1 or SQLite reports
  is passed on: D1's is cut short, and a syntax error's text can still quote
  part of a row.
- **The target.** The tool writes only to a target that holds no table, and D1
  applies nothing of a failed import. A sequence that changes a database that
  already held data is a defect in what the tool promises. Report it as a bug,
  or privately if you think it matters more than that.
